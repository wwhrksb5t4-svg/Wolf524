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
DEX_LATEST_PROFILES = f"{core.DEXSCREENER_API}/token-profiles/latest/v1"
DEX_LATEST_BOOSTS = f"{core.DEXSCREENER_API}/token-boosts/latest/v1"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
DISCOVERY_INTERVAL = max(120, int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "300")))
MAX_SCAN_PER_CYCLE = max(1, min(15, int(os.getenv("MAX_SCAN_PER_CYCLE", "8"))))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_CANDIDATE_LIQUIDITY_USD", "5000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_CANDIDATE_VOLUME_24H_USD", "1000"))
MIN_TRADES_24H = int(os.getenv("MIN_CANDIDATE_TRADES_24H", "10"))
MAX_RISK_SCORE = int(os.getenv("MAX_CANDIDATE_RISK_SCORE", "50"))
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


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value[:250]


def profile_links(profile: dict[str, Any] | None) -> list[tuple[str, str]]:
    links: dict[str, str] = {}
    for item in (profile or {}).get("links") or []:
        if isinstance(item, dict):
            url = _safe_url(item.get("url"))
            if url:
                links[url] = str(item.get("type") or "رابط عام")[:35]
    return [(label, url) for url, label in list(links.items())[:5]]


def _mint_from_gecko_pool(pool: dict[str, Any]) -> str | None:
    relationships = pool.get("relationships") or {}
    token = ((relationships.get("base_token") or {}).get("data") or {}).get("id")
    if not token:
        token = ((relationships.get("quote_token") or {}).get("data") or {}).get("id")
    if not isinstance(token, str):
        return None
    # GeckoTerminal resource IDs usually have the form "solana_<mint>".
    mint = token.split("_", 1)[1] if token.startswith("solana_") else token
    return mint if core.validate_mint(mint) else None


async def _fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def discovery_sources(client: httpx.AsyncClient) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
    """Gather candidates from latest profiles, boosts, and GeckoTerminal new pools.

    Providers are independently best-effort: a failure on one source does not suppress others.
    Their APIs are indexed snapshots, not a complete or guaranteed real-time chain feed.
    """
    sources: dict[str, set[str]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    urls = (DEX_LATEST_PROFILES, DEX_LATEST_BOOSTS, GECKO_NEW_POOLS)
    results = await asyncio.gather(*(_fetch_json(client, url) for url in urls), return_exceptions=True)
    names = ("DexScreener profiles", "DexScreener boosts", "GeckoTerminal new pools")
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            logger.warning("Discovery source unavailable (%s): %s", name, result)
            continue
        records = result.get("data", []) if isinstance(result, dict) else result
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            attrs = record.get("attributes") or {}
            chain = record.get("chainId") or attrs.get("network") or attrs.get("network_id")
            if name == "GeckoTerminal new pools":
                mint = _mint_from_gecko_pool(record)
            else:
                mint = str(record.get("tokenAddress") or "").strip()
                if chain not in {None, "solana"}:
                    continue
                if record.get("chainId") != "solana":
                    # Reject records explicitly belonging to another chain; allow absent chain only
                    # when the record came from Gecko, which is handled above.
                    continue
                if name == "DexScreener profiles":
                    profiles[mint] = record
            if mint and core.validate_mint(mint):
                sources.setdefault(mint, set()).add(name)
    return sources, profiles


def is_candidate(report: core.TokenReport) -> bool:
    trades = (report.buys_24h or 0) + (report.sells_24h or 0)
    return (
        report.risk_score <= MAX_RISK_SCORE
        and report.liquidity_usd is not None and report.liquidity_usd >= MIN_LIQUIDITY_USD
        and report.volume_24h is not None and report.volume_24h >= MIN_VOLUME_24H_USD
        and trades >= MIN_TRADES_24H
    )


def rank_key(report: core.TokenReport) -> tuple[float, ...]:
    # Sort as a research shortlist: lower heuristic risk first, then stronger observed liquidity/volume.
    return (report.risk_score, -(report.liquidity_usd or 0), -(report.volume_24h or 0))


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
                report.community_links = profile_links(profiles.get(mint))
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
    link_lines = "\n".join(f"• {label}: {url}" for label, url in links) or "• لم تُدرج روابط مجتمع في ملف DexScreener."
    return (
        f"🆕 مرشح مبكر للمراجعة — {report.name} ({report.symbol})\n"
        f"مؤشر المخاطر الأولي: {report.risk_score}/100 · السيولة: {core.usd(report.liquidity_usd)}\n"
        f"حجم 24س: {core.usd(report.volume_24h)} · معاملات شراء/بيع: {report.buys_24h}/{report.sells_24h}\n"
        f"عمر زوج التداول التقريبي: {core.age_text(report.pair_age_hours)}\n"
        f"اكتُشف عبر: {sources}\n"
        f"روابط عامة للمشروع:\n{link_lines}\n"
        f"العقد: {report.mint}\n"
        f"DexScreener: https://dexscreener.com/solana/{report.mint}\n"
        f"GeckoTerminal: https://www.geckoterminal.com/solana/pools/{report.pair_address or report.mint}\n"
        f"Solscan: https://solscan.io/token/{report.mint}\n"
        f"Phantom: {core.phantom_token_url(report.mint)}\n\n"
        "هذه قائمة بحث مبكر وليست توصية شراء. مصادر الفهرسة قد تتأخر أو تفوّت عملات، ولا يمكن ضمان أنك أول من يدخل. الروابط العامة لا تثبت نشاط أو صدق المجتمع، وقد تخسر كامل المبلغ."
    )


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("أبحث في DexScreener (الملفات الحديثة والـ boosts) وGeckoTerminal (المسابح الجديدة)، ثم أفحص المرشحين…")
    try:
        async with httpx.AsyncClient(timeout=core.HTTP_TIMEOUT) as client:
            reports, examined = await scan_discovery(client, include_seen=True)
        if not reports:
            await update.message.reply_text(f"لم يطابق مرشح الفلاتر هذه الجولة (فُحص {examined}). جرّب لاحقًا؛ هذه المصادر ليست شاملة.")
            return
        reports.sort(key=rank_key)
        for report in reports[:5]:
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
        await update.message.reply_text(f"فُعّلت التنبيهات. البحث كل {DISCOVERY_INTERVAL // 60} دقيقة تقريبًا، ضمن حدود المصادر العامة. استخدم /autodiscover off للإيقاف.")
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
    logger.info("Wolf524 multi-source discovery bot starting; polling every %s seconds", DISCOVERY_INTERVAL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
