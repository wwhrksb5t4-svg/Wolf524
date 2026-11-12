import logging
import os
import re
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler

import bot as core
import runner as discovery

logger = logging.getLogger("wolf524")
SPAM_FILTER_ENABLED = os.getenv("SPAM_FILTER_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
SPAM_FILTER_THRESHOLD = max(1, min(100, int(os.getenv("SPAM_FILTER_THRESHOLD", "50"))))

# These are lightweight warning signals, not proof that a token is fraudulent.
_SUSPICIOUS_TERMS = re.compile(
    r"\b(airdrop|claim|free|giveaway|presale|bonus|100x|1000x|pump|send\s+sol)\b",
    re.IGNORECASE,
)
_REPEAT_PATTERN = re.compile(r"(.)\1{4,}")


def spam_signals(report: Any) -> tuple[int, list[str]]:
    """Return an explainable heuristic spam score (0-100) for discovery candidates."""
    score = 0
    signals: list[str] = []
    name = str(getattr(report, "name", "") or "")
    symbol = str(getattr(report, "symbol", "") or "")
    label = f"{name} {symbol}".strip()
    normalized = label.lower()

    if _SUSPICIOUS_TERMS.search(normalized):
        score += 30
        signals.append("اسم/رمز يحتوي عبارة ترويجية شائعة في التوكنات المزعجة")
    if _REPEAT_PATTERN.search(label):
        score += 20
        signals.append("تكرار غير معتاد للحروف في الاسم أو الرمز")
    if len(symbol.strip()) > 14 or len(name.strip()) > 55:
        score += 10
        signals.append("اسم أو رمز طويل بشكل غير معتاد")

    liquidity = getattr(report, "liquidity_usd", None) or 0.0
    volume = getattr(report, "volume_24h", None) or 0.0
    if liquidity > 0:
        volume_ratio = volume / liquidity
        if volume_ratio >= 20:
            score += 25
            signals.append("حجم التداول مبالغ فيه جدًا مقارنة بالسيولة")
        elif volume_ratio >= 10:
            score += 15
            signals.append("حجم التداول مرتفع جدًا مقارنة بالسيولة")

    trades = (getattr(report, "buys_24h", None) or 0) + (getattr(report, "sells_24h", None) or 0)
    if trades < 15:
        score += 15
        signals.append("نشاط التداول ضعيف جدًا")

    concentration = getattr(report, "top10_percent", None)
    if concentration is not None and concentration >= 75:
        score += 20
        signals.append("تركيز شديد لدى أكبر 10 حسابات")
    elif concentration is not None and concentration >= 60:
        score += 10
        signals.append("تركيز مرتفع لدى أكبر 10 حسابات")

    if getattr(report, "mint_authority", None) is True:
        score += 10
        signals.append("صلاحية Mint ما زالت فعالة")
    if getattr(report, "freeze_authority", None) is True:
        score += 10
        signals.append("صلاحية Freeze ما زالت فعالة")

    return min(score, 100), signals


def install_spam_filter() -> None:
    """Filter discovery candidates using a conservative, explainable heuristic."""
    original_qualifies = discovery.qualifies
    original_format = discovery.format_candidate

    def spam_aware_qualifies(report: Any, rug: dict[str, Any] | None) -> bool:
        score, signals = spam_signals(report)
        report.spam_score = score
        report.spam_signals = signals
        return original_qualifies(report, rug) and (
            not SPAM_FILTER_ENABLED or score < SPAM_FILTER_THRESHOLD
        )

    def format_with_spam_summary(report: Any) -> str:
        message = original_format(report)
        score = getattr(report, "spam_score", None)
        signals = getattr(report, "spam_signals", [])
        if score is None:
            score, signals = spam_signals(report)
        details = "؛ ".join(signals[:3]) if signals else "لم تظهر إشارات Spam قوية ضمن البيانات المتاحة"
        return f"🧹 مؤشر Spam heuristic: {score}/100\n{details}\n\n{message}"

    discovery.qualifies = spam_aware_qualifies
    discovery.format_candidate = format_with_spam_summary


async def configure_command_menu(application: Application) -> None:
    """Publish the bot's slash-command menu in default and Arabic locales."""
    commands = [
        BotCommand("start", "بدء البوت وعرض طريقة الاستخدام"),
        BotCommand("help", "عرض المساعدة وقائمة الأوامر"),
        BotCommand("status", "التحقق من حالة البوت"),
        BotCommand("scan", "فحص توكن Solana: /scan <mint>"),
        BotCommand("watch", "إضافة توكن لقائمة المراقبة: /watch <mint>"),
        BotCommand("unwatch", "إزالة توكن من المراقبة: /unwatch <mint>"),
        BotCommand("watchlist", "عرض قائمة التوكنات المراقبة"),
        BotCommand("top", "ترتيب التوكنات المراقبة حسب المخاطر"),
        BotCommand("discover", "البحث عن توكنات جديدة مع فلتر Spam"),
        BotCommand("autodiscover", "تشغيل/إيقاف تنبيهات الاكتشاف: on أو off"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_my_commands(commands, language_code="ar")
    logger.info("Telegram slash-command menu configured")


def main() -> None:
    if not core.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Add it as a Railway service variable and redeploy.")

    install_spam_filter()
    core.init_db()
    discovery.init_db()
    app = Application.builder().token(core.BOT_TOKEN).post_init(configure_command_menu).build()

    handlers = [
        ("start", core.start_command),
        ("help", core.help_command),
        ("status", core.status_command),
        ("scan", core.scan_command),
        ("watch", core.watch_command),
        ("unwatch", core.unwatch_command),
        ("watchlist", core.watchlist_command),
        ("top", core.top_command),
        ("discover", discovery.discover_command),
        ("autodiscover", discovery.autodiscover_command),
    ]
    for name, callback in handlers:
        app.add_handler(CommandHandler(name, callback))

    if not app.job_queue:
        raise SystemExit("JobQueue unavailable; install the job-queue extra.")
    app.job_queue.run_repeating(
        discovery.discovery_job,
        interval=discovery.DISCOVERY_INTERVAL,
        first=30,
        name="token-discovery",
    )
    app.job_queue.run_repeating(
        core.monitor_watched,
        interval=core.SCAN_INTERVAL_SECONDS,
        first=core.SCAN_INTERVAL_SECONDS,
        name="watchlist-monitor",
    )
    logger.info(
        "Wolf524 starting; Birdeye enabled=%s; spam filter enabled=%s threshold=%s",
        bool(discovery.BIRDEYE_API_KEY), SPAM_FILTER_ENABLED, SPAM_FILTER_THRESHOLD,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
