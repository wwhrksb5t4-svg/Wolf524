import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import bot as core

logger = logging.getLogger("wolf524.discovery")
DISCOVERY_URL = f"{core.DEXSCREENER_API}/token-profiles/latest/v1"
DISCOVERY_INTERVAL = max(900, int(__import__("os").getenv("DISCOVERY_INTERVAL_SECONDS", "900")))
MAX_SCAN_PER_CYCLE = 5
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


async def latest_solana_profiles(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(DISCOVERY_URL)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and item.get("chainId") == "solana"]


def is_candidate(report: core.TokenReport) -> bool:
    # Conservative screening filters, not an investment recommendation or a guarantee of safety.
    trades = (report.buys_24h or 0) + (report.sells_24h or 0)
    return (
        report.risk_score <= 35
        and report.liquidity_usd is not None and report.liquidity_usd >= 15_000
        and report.volume_24h is not None and report.volume_24h >= 5_000
        and trades >= 20
    )


async def scan_discovery(client: httpx.AsyncClient, include_seen: bool = False) -> tuple[list[core.TokenReport], int]:
    async with _discovery_lock:
        profiles = await latest_solana_profiles(client)
        reports: list[core.TokenReport] = []
        examined = 0
        for profile in profiles:
            mint = str(profile.get("tokenAddress") or "").strip()
            if not core.validate_mint(mint) or (not include_seen and was_seen(mint)):
                continue
            try:
                report = await core.scan_token(mint, client)
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
    return (
        f"🆕 *مرشح للمراجعة — {core._escape_md(report.name)} ({core._escape_md(report.symbol)})*\n"
        f"مؤشر المخاطر: {report.risk_score}/100 · السيولة: {core.usd(report.liquidity_usd)}\n"
        f"حجم 24س: {core.usd(report.volume_24h)} · شراء/بيع: {report.buys_24h}/{report.sells_24h}\n"
        f"العقد: `{report.mint}`\n"
        f"[DexScreener](https://dexscreener.com/solana/{report.mint}) · "
        f"[Solscan](https://solscan.io/token/{report.mint})\n\n"
        "هذه نتيجة فلترة آلية أولية وليست توصية شراء؛ قد تكون العملة احتيالية أو غير قابلة للبيع. افحص بنفسك ولا تستثمر ما لا تتحمل خسارته."
    )


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("أبحث عن توكنات Solana الجديدة وأفحص حتى 5 منها…")
    try:
        async with httpx.AsyncClient(timeout=core.HTTP_TIMEOUT) as client:
            reports, examined = await scan_discovery(client, include_seen=True)
        if not reports:
            await update.message.reply_text(
                f"لم يظهر مرشح يطابق شروط الفلترة في هذه الجولة (فُحص {examined}). "
                "هذا لا يعني أن العملات الأخرى آمنة أو سيئة؛ جرّب لاحقًا."
            )
            return
        reports.sort(key=lambda item: (item.risk_score, -(item.liquidity_usd or 0)))
        for report in reports[:5]:
            await update.message.reply_text(candidate_summary(report), parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as exc:
        logger.exception("Discovery failed")
        await update.message.reply_text(f"تعذر جلب قائمة العملات الآن: {exc}")


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
        await update.message.reply_text(
            "تم تفعيل التنبيهات ✅ سأفحص توكنات Solana الجديدة كل 15 دقيقة تقريبًا، "
            "وأرسل ما يطابق شروط الفلترة فقط. استخدم /autodiscover off للإيقاف."
        )
    else:
        await update.message.reply_text("تم إيقاف تنبيهات الاكتشاف التلقائي.")


async def discovery_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    targets = subscriber_ids()
    if not targets:
        return
    try:
        async with httpx.AsyncClient(timeout=core.HTTP_TIMEOUT) as client:
            reports, _ = await scan_discovery(client)
        for report in reports:
            for user_id in targets:
                try:
                    await context.bot.send_message(user_id, candidate_summary(report), parse_mode="Markdown", disable_web_page_preview=True)
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
    app.add_handler(CommandHandler("start", core.start_command))
    app.add_handler(CommandHandler("help", core.help_command))
    app.add_handler(CommandHandler("status", core.status_command))
    app.add_handler(CommandHandler("scan", core.scan_command))
    app.add_handler(CommandHandler("watch", core.watch_command))
    app.add_handler(CommandHandler("unwatch", core.unwatch_command))
    app.add_handler(CommandHandler("watchlist", core.watchlist_command))
    app.add_handler(CommandHandler("top", core.top_command))
    app.add_handler(CommandHandler("discover", discover_command))
    app.add_handler(CommandHandler("autodiscover", autodiscover_command))
    if not app.job_queue:
        raise SystemExit("JobQueue is unavailable. Install requirements.txt with the job-queue extra.")
    app.job_queue.run_repeating(discovery_job, interval=DISCOVERY_INTERVAL, first=60, name="automatic-token-discovery")
    if core.SCAN_INTERVAL_SECONDS:
        app.job_queue.run_repeating(core.monitor_watched, interval=core.SCAN_INTERVAL_SECONDS,
                                    first=core.SCAN_INTERVAL_SECONDS, name="watchlist-monitor")
    logger.info("Wolf524 discovery bot is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
