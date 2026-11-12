import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import bot as core

logger = logging.getLogger("wolf524.discovery")
DEX_LATEST_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_LATEST_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
BIRDEYE_NEW_TOKENS = "https://public-api.birdeye.so/defi/token新-created"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1/tokens"
DISCOVERY_INTERVAL = max(120, int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "300")))
MAX_SCAN_PER_CYCLE = max(1, min(15, int(os.getenv("MAX_SCAN_PER_CYCLE", "8"))))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_CANDIDATE_LIQUIDITY_USD", "5000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_CANDIDATE_VOLUME_24H_USD", "1000"))
MIN_TRADES_24H = int(os.getenv("MIN_CANDIDATE_TRADES_24H", "10"))
MAX_RISK_SCORE = int(os.getenv("MAX_CANDIDATE_RISK_SCORE", "50"))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()
_discovery_lock = asyncio.Lock()


def init_discovery_db() -> None:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        db.execute("CREATE TABLE IF NOT EXISTS discovery_subscribers (user_id INTEGER PRIMARY KEY, enabled_at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS discovery_seen (mint TEXT PRIMARY KEY, first_seen TEXT NOT NULL, risk_score INTEGER, liquidity_usd REAL)")
        db.commit()


def subscriber_ids() -> list[int]:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        return [row[0] for row in db.execute("SELECT user_id FROM discovery_subscribers")]


def set_subscription(user_id: int, enabled: bool) -> None:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        if enabled:
            db.execute("INSERT OR REPLACE INTO discovery_subscribers (user_id, enabled_at) VALUES (?, ?)",
                       (user_id, datetime.now(timezone.utc).isoformat()))
        else:
            db.execute("DELETE FROM discovery_subscribers WHERE user_id = ?", (user_id,))
        db.commit()


def was_seen(mint: str) -> bool:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        return db.execute("SELECT 1 FROM discovery_seen WHERE mint = ?", (mint,)).fetchone() is not None


def mark_seen(report: core.TokenReport) -> None:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        db.execute("INSERT OR IGNORE INTO discovery_seen (mint, first_seen, risk_score, liquidity_usd) VALUES (?, ?, ?, ?)",
                   (report.mint, datetime.now(timezone.utc).isoformat(), report.risk_score, report.liquidity_usd))
        db.commit()


def _valid_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urlparse(value)
    return value[:250] if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("tokens", "items", "tokenList", "list"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
    return []


def _extract_mint(record: dict[str, Any], source: str) -> str | None:
    if source == "GeckoTerminal new pools":
        rels = record.get("relationships") or {}
        for name in ("base_token", "quote_token"):
            token = (((rels.get(name) or {}).get("data") or {}).get("id"))
            if isinstance(token, str):
                mint = token.split("_", 1)[1] if token.startswith("solana_") else token
                if core.validate_mint(mint):
                    return mint
        return None
    candidate = record.get("tokenAddress") or record.get("address") or record.get("address1") or record.get("mint")
    if isinstance(candidate, str) and core.validate_mint(candidate.strip()):
        return candidate.strip()
    return None


def _profile_links(profile: dict[str, Any] | None) -> list[tuple[str, str]]:
    out: dict[str, str] = {}
    for link in (profile or {}).get("links") or []:
        if isinstance(link, dict):
            url = _valid_url(link.get("url"))
            if url:
                out[url] = str(link.get("type") or "رابط عام")[:35]
    return [(label, url) for url, label in list(out.items())[:5]]


async def _json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response.json()


async def _source(client: httpx.AsyncClient, name: str, url: str, headers: dict[str, str] | None = None) -> tuple[str, Any]:
    try:
        return name, await _json(client, url, headers=headers or {})
    except Exception as exc:
        logger.warning("Discovery source unavailable (%s): %s", name, exc)
        return name, exc


async def discovery_sources(client: httpx.AsyncClient) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
    jobs = [
        _source(client, "DexScreener profiles", DEX_LATEST_PROFILES),
        _source(client, "DexScreener boosts", DEX_LATEST_BOOSTS),
        _source(client, "GeckoTerminal new pools", GECKO_NEW_POOLS),
    ]
    if BIRDEYE_API_KEY:
        jobs.append(_source(client, "Birdeye new tokens", BIRDEYE_NEW_TOKENS,
                            headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}))
    results = await asyncio.gather(*jobs)
    sources: dict[str, set[str]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    for name, payload in results:
        if isinstance(payload, Exception):
            continue
        for record in _records(payload):
            if name.startswith("DexScreener"):
                if record.get("chainId") != "solana":
                    continue
                if name == "DexScreener profiles":
                    mint = _extract_mint(record, name)
                    if mint:
                        profiles[mint] = record
            else:
                mint = _extract_mint(record, name)
            if mint:
                sources.setdefault(mint, set()).add(name)
    if not sources:
        logger.warning("Discovery sources returned no Solana mints")
    return sources, profiles


def _risk_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    items = report.get("risks") or report.get("riskItems") or []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


async def rugcheck_report(client: httpx.AsyncClient, mint: str) -> dict[str, Any] | None:
    """Best-effort RugCheck enrichment. No API key is embedded; endpoint/schema may change."""
    try:
        data = await _json(client, f"{RUGCHECK_BASE}/{mint}/report/summary")
        if isinstance(data, dict):
            return data
    except httpx.HTTPStatusError as exc:
        # Try the full report only for an endpoint-not-found response.
        if exc.response.status_code != 404:
            logger.info("RugCheck summary unavailable for %s: %s", mint, exc)
            return None
    except Exception as exc:
        logger.info("RugCheck unavailable for %s: %s", mint, exc)
        return None
    try:
        data = await _json(client, f"{RUGCHECK_BASE}/{mint}/report")
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.info("RugCheck full report unavailable for %s: %s", mint, exc)
        return None


def _rugcheck_score(report: dict[str, Any] | None) -> tuple[int | None, list[str]]:
    if not report:
        return None, []
    raw = report.get("score")
    try:
        score = int(float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        score = None
    risks = []
    for item in _risk_items(report)[:5]:
        description = item.get("description") or item.get("name") or item.get("value")
        if description:
            risks.append(str(description)[:180])
    return score, risks


def is_candidate(report: core.TokenReport) -> bool:
    trades = (report.buys_24h or 0) + (report.sells_24h or 0)
    return (
        report.risk_score <= MAX_RISK_SCORE
        and report.liquidity_usd is not None and report.liquidity_usd >= MIN_LIQUIDITY_USD
        and report.volume_24h is not None and report.volume_24h >= MIN_VOLUME_24H_USD
        and trades >= MIN_TRADES_24H
    )


def rank_key(report: core.TokenReport) -> tuple[float, ...]:
    rug_score = getattr(report, "rugcheck_score", None)
    # RugCheck scores are displayed as a separate provider signal, not mixed with local score
    # because provider scales and meanings may differ.
    return (report.risk_score, -(report.liquidity_usd or 0), -(report.volume_24h or 0), float(rug_score or 0))


async def scan_discovery(client: httpx.AsyncClient, include_seen: bool = False) -> tuple[list[core.TokenReport], int]:
    async with _discovery_lock:
        candidates, profiles = await discovery_sources(client)
        reports: list[core.TokenReport] = []
        examined = 0
        for mint, source_names in candidates.items():
            if not include_seen and was_seen(mint):
                continue
            try:
                report = await core.scan_token(mint, client)
                report.discovery_sources = sorted(source_names)
                report.community_links = _profile_links(profiles.get(mint))
                report.rugcheck = await rugcheck_report(client, mint)
                report.rugcheck_score, report.rugcheck_risks = _rugcheck_score(report.rugcheck)
                mark_seen(report)
                examined += 1
                if is_candidate(report):
                    reports.append(report)
            except Exception as exc:
                logger.info("Discovery scan failed for %s: %s", mint, exc)
            if examined >= MAX_SCAN_PER_CYCLE:
                break
            await asyncio.sleep(0.25)
        return reports, examined


def candidate_summary(report: core.TokenReport) -> str:
    sources = ", ".join(getattr(report, "discovery_sources", [])) or "مصدر عام"
    links = getattr(report, "community_links", [])
    links_text = "\n".join(f"• {label}: {url}" for label, url in links) or "• لا توجد روابط مجتمع متاحة في المصدر."
    rug_score = getattr(report, "rugcheck_score", None)
    rug_risks = getattr(report, "rugcheck_risks", [])
    rug_text = f"RugCheck score: {rug_score}" if rug_score is not None else "RugCheck: غير متاح"
    if rug_risks:
        rug_text += "\nملاحظات RugCheck: " + "؛ ".join(rug_risks[:3])
    phantom_link = getattr(core, "phantom_token_url", lambda address: f"https://phantom.app/ul/browse/https%3A%2F%2Fdexscreener.com%2Fsolana%2F{address}")(report.mint)
    return (
        f"🆕 مرشح للمراجعة — {report.name} ({report.symbol})\n"
        f"مؤشر المخاطر المحلي: {report.risk_score}/100 · السيولة: {core.usd(report.liquidity_usd)}\n"
        f"حجم 24س: {core.usd(report.volume_24h)} · معاملات شراء/بيع: {report.buys_24h}/{report.sells_24h}\n"
        f"مصادر الاكتشاف: {sources}\n{rug_text}\n"
        f"روابط المشروع المنشورة:\n{links_text}\n"
        f"Mint: {report.mint}\n"
        f"DexScreener: https://dexscreener.com/solana/{report.mint}\n"
        f"GeckoTerminal: https://www.geckoterminal.com/solana/pools/{report.pair_address or report.mint}\n"
        f"Solscan: https://solscan.io/token/{report.mint}\n"
        f"Phantom: {phantom_link}\n\n"
        "هذه قائمة فرز أولية وليست توصية شراء. RugCheck قد يتعذر أو تتغير دلالات نتيجته؛ الروابط لا تثبت نشاط المجتمع أو شرعيته. لا تضمن المصادر اكتشاف كل الإطلاقات أو أنك أول من يدخل، وقد تخسر كامل المبلغ."
    )


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("أبحث في DexScreener وGeckoTerminal" + (" وBirdeye" if BIRDEYE_API_KEY else "") + "، ثم أفحص المرشحين مع RugCheck…")
    try:
        async with httpx.AsyncClient(timeout=core.HTTP_TIMEOUT) as client:
            reports, examined = await scan_discovery(client, include_seen=True)
        if not reports:
            await update.message.reply_text(f"لم يطابق مرشح الفلاتر هذه الجولة (فُحص {examined}). جرّب لاحقًا؛ المصادر ليست شاملة.")
            return
        for report in sorted(reports, key=rank_key)[:5]:
            await update.message.reply_text(candidate_summary(report), disable_web_page_preview=True)
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
    set_subscription(update.effective_user.id, enabled)
    if enabled:
        await update.message.reply_text(f"تم تفعيل التنبيهات. البحث كل {DISCOVERY_INTERVAL // 60} دقيقة تقريبًا. استخدم /autodiscover off للإيقاف.")
    else:
        await update.message.reply_text("تم إيقاف تنبيهات الاكتشاف.")


async def discovery_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    targets = subscriber_ids()
    if not targets:
        return
    try:
        async with httpx.AsyncClient(timeout=core.HTTP_TIMEOUT) as client:
            reports, _ = await scan_discovery(client)
        for report in sorted(reports, key=rank_key)[:5]:
            for user_id in targets:
                try:
                    await context.bot.send_message(user_id, candidate_summary(report), disable_web_page_preview=True)
                except Exception:
                    logger.exception("Could not send discovery alert to %s", user_id)
    except Exception:
        logger.exception("Automatic discovery job failed")


def main() -> None:
    if not core.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Add it as a Railway service variable and redeploy.")
    core.init_db()
    init_discovery_db()
    app = Application.builder().token(core.BOT_TOKEN).build()
    for name, callback in (("start", core.start_command), ("help", core.help_command), ("status", core.status_command),
                           ("scan", core.scan_command), ("watch", core.watch_command),
                           ("unwatch", core.unwatch_command), ("watchlist", core.watchlist_command),
                           ("top", core.top_command)):
        app.add_handler(CommandHandler(name, callback))
    app.add_handler(CommandHandler("discover", discover_command))
    app.add_handler(CommandHandler("autodiscover", autodiscover_command))
    if not app.job_queue:
        raise SystemExit("JobQueue is unavailable. Install requirements.txt with the job-queue extra.")
    app.job_queue.run_repeating(discovery_job, interval=DISCOVERY_INTERVAL, first=30, name="automatic-token-discovery")
    app.job_queue.run_repeating(core.monitor_watched, interval=core.SCAN_INTERVAL_SECONDS,
                                first=core.SCAN_INTERVAL_SECONDS, name="watchlist-monitor")
    logger.info("Wolf524 multi-source discovery starting; Birdeye enabled=%s", bool(BIRDEYE_API_KEY))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
