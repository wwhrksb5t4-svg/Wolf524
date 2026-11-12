import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import bot as core

logger = logging.getLogger("wolf524.discovery")
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
BIRDEYE_NEW_LISTINGS = "https://public-api.birdeye.so/public/token/new-listing"

DISCOVERY_INTERVAL = max(120, int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "300")))
MAX_SCAN_PER_CYCLE = max(1, min(12, int(os.getenv("MAX_SCAN_PER_CYCLE", "6"))))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_CANDIDATE_LIQUIDITY_USD", "5000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_CANDIDATE_VOLUME_24H_USD", "1000"))
MIN_TRADES_24H = int(os.getenv("MIN_CANDIDATE_TRADES_24H", "10"))
MAX_LOCAL_RISK = int(os.getenv("MAX_CANDIDATE_RISK_SCORE", "50"))
RESCAN_MINUTES = max(5, int(os.getenv("DISCOVERY_RESCAN_MINUTES", "15")))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()
GECKO_API_KEY = os.getenv("GECKO_API_KEY", "").strip()
_lock = asyncio.Lock()


def init_db() -> None:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS discovery_subscribers (
            user_id INTEGER PRIMARY KEY, enabled_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS discovery_seen (
            mint TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_checked TEXT,
            alerted INTEGER NOT NULL DEFAULT 0, risk_score INTEGER, liquidity_usd REAL)""")
        cols = {row[1] for row in db.execute("PRAGMA table_info(discovery_seen)")}
        if "last_checked" not in cols:
            db.execute("ALTER TABLE discovery_seen ADD COLUMN last_checked TEXT")
        if "alerted" not in cols:
            db.execute("ALTER TABLE discovery_seen ADD COLUMN alerted INTEGER NOT NULL DEFAULT 0")
        db.execute("""CREATE TABLE IF NOT EXISTS market_history (
            mint TEXT NOT NULL, captured_at TEXT NOT NULL, price_usd REAL,
            liquidity_usd REAL, volume_24h REAL, PRIMARY KEY (mint, captured_at))""")
        db.commit()


def subscribers() -> list[int]:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        return [r[0] for r in db.execute("SELECT user_id FROM discovery_subscribers")]


def subscribe(user_id: int, enabled: bool) -> None:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        if enabled:
            db.execute("INSERT OR REPLACE INTO discovery_subscribers VALUES (?, ?)",
                       (user_id, datetime.now(timezone.utc).isoformat()))
        else:
            db.execute("DELETE FROM discovery_subscribers WHERE user_id=?", (user_id,))
        db.commit()


def seen_status(mint: str) -> tuple[bool, bool]:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        row = db.execute("SELECT last_checked, alerted FROM discovery_seen WHERE mint=?", (mint,)).fetchone()
    if not row:
        return False, False
    last_checked, alerted = row
    recent = False
    if last_checked:
        try:
            recent = datetime.fromisoformat(last_checked) > datetime.now(timezone.utc) - timedelta(minutes=RESCAN_MINUTES)
        except ValueError:
            pass
    return recent, bool(alerted)


def save_scan(report: core.TokenReport, candidate: bool, old_liquidity: float | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(core.DATABASE_PATH) as db:
        db.execute("""INSERT INTO discovery_seen
            (mint, first_seen, last_checked, alerted, risk_score, liquidity_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET last_checked=excluded.last_checked,
            alerted=MAX(discovery_seen.alerted, excluded.alerted),
            risk_score=excluded.risk_score, liquidity_usd=excluded.liquidity_usd""",
            (report.mint, now, now, int(candidate), report.risk_score, report.liquidity_usd))
        db.execute("INSERT OR REPLACE INTO market_history VALUES (?, ?, ?, ?, ?)",
                   (report.mint, now, report.price_usd, report.liquidity_usd, report.volume_24h))
        db.commit()


def previous_liquidity(mint: str) -> float | None:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        row = db.execute("SELECT liquidity_usd FROM market_history WHERE mint=? ORDER BY captured_at DESC LIMIT 1",
                         (mint,)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _valid_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    return value.strip()[:250] if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("tokens", "items", "tokenList", "token_list", "list"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


def _mint(record: dict[str, Any], source: str) -> str | None:
    if source == "GeckoTerminal new pools":
        rel = record.get("relationships") or {}
        token_id = (((rel.get("base_token") or {}).get("data") or {}).get("id"))
        if isinstance(token_id, str):
            value = token_id.split("_", 1)[-1]
            return value if core.validate_mint(value) else None
        return None
    value = record.get("tokenAddress") or record.get("address") or record.get("mint")
    if isinstance(value, str) and core.validate_mint(value.strip()):
        return value.strip()
    return None


async def _get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response.json()


async def get_source(client: httpx.AsyncClient, name: str, url: str, **kwargs: Any) -> tuple[str, Any]:
    try:
        return name, await _get(client, url, **kwargs)
    except Exception as exc:
        logger.warning("Discovery source unavailable (%s): %s", name, exc)
        return name, exc


async def collect_candidates(client: httpx.AsyncClient) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
    jobs = [
        get_source(client, "DexScreener profiles", DEX_PROFILES),
        get_source(client, "DexScreener boosts", DEX_BOOSTS),
        get_source(client, "GeckoTerminal new pools", GECKO_NEW_POOLS,
                   headers={"Accept": "application/json;version=20230302"}),
    ]
    if BIRDEYE_API_KEY:
        jobs.append(get_source(
            client, "Birdeye new listings", BIRDEYE_NEW_LISTINGS,
            params={"limit": 20, "meme_platform_enabled": "true"},
            headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"},
        ))
    results = await asyncio.gather(*jobs)
    candidates: dict[str, set[str]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    for name, payload in results:
        if isinstance(payload, Exception):
            continue
        for record in _records(payload):
            if name.startswith("DexScreener") and record.get("chainId") != "solana":
                continue
            mint = _mint(record, name)
            if not mint:
                continue
            candidates.setdefault(mint, set()).add(name)
            if name == "DexScreener profiles":
                profiles[mint] = record
    logger.info("Collected %d unique candidate mints from %d discovery sources", len(candidates), len(results))
    return candidates, profiles


def community_links(profile: dict[str, Any] | None) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for item in (profile or {}).get("links") or []:
        if isinstance(item, dict):
            url = _valid_url(item.get("url"))
            if url:
                found[url] = str(item.get("type") or "رابط عام")[:35]
    return [(label, url) for url, label in list(found.items())[:5]]


async def rugcheck(client: httpx.AsyncClient, mint: str) -> dict[str, Any] | None:
    # Best-effort provider enrichment; unavailable data is never interpreted as a clean result.
    for suffix in ("report/summary", "report"):
        try:
            value = await _get(client, f"https://api.rugcheck.xyz/v1/tokens/{mint}/{suffix}")
            if isinstance(value, dict):
                return value
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                continue
            logger.info("RugCheck unavailable: %s", exc)
            return None
        except Exception as exc:
            logger.info("RugCheck unavailable: %s", exc)
            return None
    return None


def risk_notes(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return []
    risks = data.get("risks") or data.get("riskItems") or []
    out = []
    if isinstance(risks, list):
        for item in risks[:6]:
            if isinstance(item, dict):
                text = item.get("description") or item.get("name") or item.get("value")
                if text:
                    out.append(str(text)[:160])
    return out


def analytics(report: core.TokenReport, pair: dict[str, Any], prior_liquidity: float | None) -> tuple[int, list[str]]:
    """Explainable market-activity score, not a return forecast or buy recommendation."""
    score = 0
    notes: list[str] = []
    liquidity = report.liquidity_usd or 0.0
    volume = report.volume_24h or 0.0
    trades = (report.buys_24h or 0) + (report.sells_24h or 0)
    change = pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    h1 = txns.get("h1") or {}
    b1, s1 = int(h1.get("buys") or 0), int(h1.get("sells") or 0)

    if liquidity >= 100_000:
        score += 20; notes.append("سيولة مرصودة أعلى نسبيًا")
    elif liquidity >= 20_000:
        score += 15; notes.append("سيولة مرصودة متوسطة")
    elif liquidity >= MIN_LIQUIDITY_USD:
        score += 8; notes.append("سيولة محدودة؛ قد يكون الانزلاق مرتفعًا")
    else:
        notes.append("السيولة دون حد الفلترة")

    vl = volume / liquidity if liquidity else 0
    if 0.2 <= vl <= 5:
        score += 15; notes.append("نسبة الحجم إلى السيولة ضمن نطاق غير متطرف")
    elif vl > 10:
        score += 2; notes.append("نسبة الحجم إلى السيولة شديدة الارتفاع؛ تحقق من التداول الوهمي")
    elif volume > 0:
        score += 7; notes.append("يوجد حجم تداول مرصود")

    if trades >= 500:
        score += 15; notes.append("نشاط معاملات يومي مرتفع")
    elif trades >= 100:
        score += 12
    elif trades >= MIN_TRADES_24H:
        score += 7
    else:
        notes.append("عدد المعاملات قليل")

    if b1 + s1 >= 10:
        share = b1 / (b1 + s1)
        if 0.35 <= share <= 0.75:
            score += 10; notes.append("نشاط الساعة الأخيرة لا يهيمن عليه اتجاه واحد")
        elif share > 0.9:
            score += 2; notes.append("شراء مهيمن بشدة في الساعة الأخيرة؛ خطر اندفاع مؤقت")
        else:
            notes.append("ضغط بيع مرصود في الساعة الأخيرة")

    if report.pair_age_hours is not None:
        if report.pair_age_hours <= 24:
            score += 8; notes.append("زوج حديث جدًا، مع مخاطر إطلاق مرتفعة")
        elif report.pair_age_hours <= 168:
            score += 5; notes.append("زوج حديث نسبيًا")

    if report.top10_percent is not None:
        if report.top10_percent < 20:
            score += 10; notes.append("تركيز أكبر 10 حسابات منخفض نسبيًا")
        elif report.top10_percent < 35:
            score += 5
        else:
            notes.append("تركيز مرتفع لدى أكبر 10 حسابات")
    else:
        notes.append("بيانات تركّز الحيازة غير متاحة")

    if report.mint_authority is False and report.freeze_authority is False:
        score += 10; notes.append("صلاحيتا Mint وFreeze غير فعالتين بحسب RPC")
    elif report.mint_authority is True or report.freeze_authority is True:
        notes.append("صلاحية Mint أو Freeze ما زالت فعالة")
    else:
        notes.append("تعذر التحقق من صلاحيات Mint/Freeze")

    if prior_liquidity and liquidity:
        delta = (liquidity - prior_liquidity) / prior_liquidity
        if delta <= -0.2:
            score -= 15; notes.append(f"هبوط سيولة {abs(delta)*100:.0f}% منذ آخر لقطة محفوظة")
        elif delta >= 0.1:
            score += 5; notes.append("زيادة سيولة منذ آخر لقطة؛ لم يتم تأكيد استمراريتها")
    p1 = change.get("h1")
    try:
        p1 = float(p1) if p1 is not None else None
    except (TypeError, ValueError):
        p1 = None
    if p1 is not None and p1 <= -20:
        score -= 10; notes.append("هبوط سعر حاد خلال ساعة وفق بيانات المصدر")
    elif p1 is not None and p1 >= 80:
        notes.append("ارتفاع سريع جدًا؛ مطاردة السعر قد تكون عالية المخاطر")
    return max(0, min(100, score)), notes


def qualifies(report: core.TokenReport, rug: dict[str, Any] | None) -> bool:
    trades = (report.buys_24h or 0) + (report.sells_24h or 0)
    notes = [x.lower() for x in risk_notes(rug)]
    severe = any(any(term in item for term in ("honeypot", "rugged", "freeze authority", "mint authority")) for item in notes)
    return (report.risk_score <= MAX_LOCAL_RISK
            and report.liquidity_usd is not None and report.liquidity_usd >= MIN_LIQUIDITY_USD
            and report.volume_24h is not None and report.volume_24h >= MIN_VOLUME_24H_USD
            and trades >= MIN_TRADES_24H and not severe)


def rank_key(report: core.TokenReport) -> tuple[float, ...]:
    return (report.risk_score, -getattr(report, "research_score", 0),
            -(report.liquidity_usd or 0), -(report.volume_24h or 0))


def phantom_url(mint: str) -> str:
    return f"https://phantom.app/ul/browse/https%3A%2F%2Fdexscreener.com%2Fsolana%2F{mint}"


async def scan_discovery(client: httpx.AsyncClient, include_seen: bool = False) -> tuple[list[core.TokenReport], int]:
    async with _lock:
        candidates, profiles = await collect_candidates(client)
        selected = []
        for mint in candidates:
            recent, alerted = seen_status(mint)
            if include_seen or (not recent and not alerted):
                selected.append(mint)
        reports: list[core.TokenReport] = []
        examined = 0
        for mint in selected[:MAX_SCAN_PER_CYCLE]:
            try:
                prior = previous_liquidity(mint)
                report = await core.scan_token(mint, client)
                pair = await core.fetch_dex_pair(client, mint) or {}
                rug = await rugcheck(client, mint)
                report.discovery_sources = sorted(candidates[mint])
                report.community_links = community_links(profiles.get(mint))
                report.rugcheck_label = rug.get("score", rug.get("riskLevel", "متاح")) if rug else "غير متاح"
                report.rugcheck_notes = risk_notes(rug)
                report.market_change = pair.get("priceChange") or {}
                report.recent_txns = (pair.get("txns") or {}).get("h1") or {}
                report.research_score, report.analysis_notes = analytics(report, pair, prior)
                is_ok = qualifies(report, rug)
                save_scan(report, is_ok, prior)
                examined += 1
                if is_ok:
                    reports.append(report)
            except Exception:
                logger.exception("Candidate scan failed for %s", mint)
            await asyncio.sleep(0.25)
        return reports, examined


def format_candidate(report: core.TokenReport) -> str:
    links = getattr(report, "community_links", [])
    link_text = "\n".join(f"• {label}: {url}" for label, url in links) or "• لا توجد روابط اجتماعية منشورة في المصدر"
    notes = getattr(report, "analysis_notes", [])
    note_text = "\n".join(f"• {item}" for item in notes[:7]) or "• التحليل محدود بسبب نقص البيانات"
    changes = getattr(report, "market_change", {})
    txns = getattr(report, "recent_txns", {})
    rug_notes = getattr(report, "rugcheck_notes", [])
    rug_text = f"RugCheck: {getattr(report, 'rugcheck_label', 'غير متاح')}"
    if rug_notes:
        rug_text += "\nملاحظات: " + "؛ ".join(rug_notes[:3])
    return (
        f"🔍 مرشح للبحث — {report.name} ({report.symbol})\n"
        f"درجة النشاط البحثية: {report.research_score}/100 · مؤشر المخاطر المحلي: {report.risk_score}/100\n"
        f"السيولة: {core.usd(report.liquidity_usd)} · الحجم 24س: {core.usd(report.volume_24h)}\n"
        f"معاملات 24س شراء/بيع: {report.buys_24h}/{report.sells_24h} · ساعة: {txns.get('buys', '?')}/{txns.get('sells', '?')}\n"
        f"تغير السعر: 1س {changes.get('h1', '?')}% · 6س {changes.get('h6', '?')}% · 24س {changes.get('h24', '?')}%\n"
        f"عمر الزوج: {core.age_text(report.pair_age_hours)} · تركيز أكبر 10 حسابات: {report.top10_percent if report.top10_percent is not None else 'غير متاح'}%\n"
        f"المصادر: {', '.join(report.discovery_sources)}\n{rug_text}\n"
        f"أسباب التقييم:\n{note_text}\nروابط المشروع المنشورة:\n{link_text}\n"
        f"Mint: {report.mint}\nDexScreener: https://dexscreener.com/solana/{report.mint}\n"
        f"Solscan: https://solscan.io/token/{report.mint}\nPhantom: {phantom_url(report.mint)}\n\n"
        "هذه أداة فرز بحثية وليست توصية شراء أو توقعًا للعائد. مصادر الفهرسة قد تتأخر أو تفوّت الإطلاقات، ولا تضمن أنك أول من يدخل. نتيجة RugCheck غير المتاحة لا تعني الأمان؛ تحقق بنفسك وقد تخسر كامل المبلغ."
    )


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("أفحص مصادر DexScreener وGeckoTerminal" + (" وBirdeye" if BIRDEYE_API_KEY else "") + "، مع مؤشرات السوق وRugCheck…")
    try:
        async with httpx.AsyncClient(timeout=core.HTTP_TIMEOUT) as client:
            reports, examined = await scan_discovery(client, include_seen=True)
        if not reports:
            await update.message.reply_text(f"لم يجتز مرشح الفلاتر هذه الجولة (فُحص {examined}). المصادر ليست شاملة؛ جرّب لاحقًا.")
            return
        for report in sorted(reports, key=rank_key)[:5]:
            await update.message.reply_text(format_candidate(report), disable_web_page_preview=True)
    except Exception as exc:
        logger.exception("Discovery failed")
        await update.message.reply_text(f"تعذر إكمال البحث: {exc}")


async def autodiscover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    option = context.args[0].lower() if context.args else ""
    if option not in {"on", "off"}:
        await update.message.reply_text("الاستخدام: /autodiscover on أو /autodiscover off")
        return
    enabled = option == "on"
    subscribe(update.effective_user.id, enabled)
    await update.message.reply_text(f"تم تفعيل تنبيهات البحث (كل {DISCOVERY_INTERVAL//60} دقيقة تقريبًا)." if enabled else "تم إيقاف تنبيهات البحث.")


async def discovery_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    recipients = subscribers()
    if not recipients:
        return
    try:
        async with httpx.AsyncClient(timeout=core.HTTP_TIMEOUT) as client:
            reports, _ = await scan_discovery(client)
        for report in sorted(reports, key=rank_key)[:5]:
            for user_id in recipients:
                try:
                    await context.bot.send_message(user_id, format_candidate(report), disable_web_page_preview=True)
                except Exception:
                    logger.exception("Could not notify user %s", user_id)
    except Exception:
        logger.exception("Discovery job failed")


def main() -> None:
    if not core.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Add it as a Railway service variable and redeploy.")
    core.init_db()
    init_db()
    app = Application.builder().token(core.BOT_TOKEN).build()
    for name, callback in (("start", core.start_command), ("help", core.help_command), ("status", core.status_command),
                           ("scan", core.scan_command), ("watch", core.watch_command),
                           ("unwatch", core.unwatch_command), ("watchlist", core.watchlist_command),
                           ("top", core.top_command)):
        app.add_handler(CommandHandler(name, callback))
    app.add_handler(CommandHandler("discover", discover_command))
    app.add_handler(CommandHandler("autodiscover", autodiscover_command))
    if not app.job_queue:
        raise SystemExit("JobQueue unavailable; install the job-queue extra.")
    app.job_queue.run_repeating(discovery_job, interval=DISCOVERY_INTERVAL, first=30, name="token-discovery")
    app.job_queue.run_repeating(core.monitor_watched, interval=core.SCAN_INTERVAL_SECONDS,
                                first=core.SCAN_INTERVAL_SECONDS, name="watchlist-monitor")
    logger.info("Wolf524 analytics starting; Birdeye enabled=%s", bool(BIRDEYE_API_KEY))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
