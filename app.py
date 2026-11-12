import logging
import os
import re
import sqlite3
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

import bot as core
import runner as discovery

logger = logging.getLogger("wolf524")
SPAM_FILTER_THRESHOLD = max(1, min(100, int(os.getenv("SPAM_FILTER_THRESHOLD", "50"))))

# These are heuristic warning indicators, not proof that a token is fraudulent.
_SUSPICIOUS_TERMS = re.compile(
    r"\b(airdrop|claim|free|giveaway|presale|bonus|100x|1000x|pump|send\s+sol)\b",
    re.IGNORECASE,
)
_REPEAT_PATTERN = re.compile(r"(.)\1{4,}")
BUY_TIPS = (
    "🛡️ قائمة تحقق بحثية عامة — ليست توصية شراء أو بيع:\n"
    "• تحقّق من عنوان Mint عبر أكثر من مصدر موثوق، ولا تعتمد على الاسم أو الرمز فقط.\n"
    "• راجع السيولة وإمكانية السحب والانزلاق، وابحث عن معلومات القفل من مصدر مستقل؛ هذا البوت لا يثبت القفل.\n"
    "• افحص صلاحيات Mint/Freeze وتركيز الحيازة ومخاطر RugCheck؛ غياب البيانات لا يعني أن التوكن آمن.\n"
    "• لا تطارد الارتفاعات الحادة، واحذر الروابط والوعود بالعائد المضمون.\n"
    "• لا تستخدم أموالًا تحتاجها أو اقتراضًا/رافعة، وحدّد مسبقًا مقدار الخسارة التي تستطيع تحمّلها.\n"
    "• الدرجات والفلاتر آلية وتقريبية وقد تخطئ أو تفوّت مخاطر؛ اتخذ قرارك بنفسك بعد بحث مستقل."
)


def init_preferences_db() -> None:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS spam_filter_preferences (
            user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)""")
        db.commit()


def spam_filter_enabled(user_id: int) -> bool:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        row = db.execute(
            "SELECT enabled FROM spam_filter_preferences WHERE user_id=?", (user_id,)
        ).fetchone()
    return bool(row[0]) if row is not None else True


def set_spam_filter(user_id: int, enabled: bool) -> None:
    with sqlite3.connect(core.DATABASE_PATH) as db:
        db.execute(
            "INSERT INTO spam_filter_preferences (user_id, enabled) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled",
            (user_id, int(enabled)),
        )
        db.commit()


def spam_signals(report: Any) -> tuple[int, list[str]]:
    """Return an explainable heuristic spam score from 0 to 100."""
    score = 0
    signals: list[str] = []
    name = str(getattr(report, "name", "") or "")
    symbol = str(getattr(report, "symbol", "") or "")
    label = f"{name} {symbol}".strip()

    if _SUSPICIOUS_TERMS.search(label):
        score += 30
        signals.append("عبارة ترويجية شائعة في الاسم أو الرمز")
    if _REPEAT_PATTERN.search(label):
        score += 20
        signals.append("تكرار غير معتاد للحروف")
    if len(symbol.strip()) > 14 or len(name.strip()) > 55:
        score += 10
        signals.append("اسم أو رمز طويل بشكل غير معتاد")

    liquidity = getattr(report, "liquidity_usd", None) or 0.0
    volume = getattr(report, "volume_24h", None) or 0.0
    if liquidity > 0:
        ratio = volume / liquidity
        if ratio >= 20:
            score += 25
            signals.append("حجم تداول مرتفع جدًا مقارنة بالسيولة")
        elif ratio >= 10:
            score += 15
            signals.append("حجم تداول مرتفع مقارنة بالسيولة")

    trades = (getattr(report, "buys_24h", None) or 0) + (getattr(report, "sells_24h", None) or 0)
    if trades < 15:
        score += 15
        signals.append("نشاط تداول ضعيف جدًا")

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


def install_spam_scoring() -> None:
    """Attach spam score to discovery candidates; filtering is applied per user."""
    original_qualifies = discovery.qualifies
    original_format = discovery.format_candidate

    def score_candidate(report: Any, rug: dict[str, Any] | None) -> bool:
        report.spam_score, report.spam_signals = spam_signals(report)
        return original_qualifies(report, rug)

    def format_candidate(report: Any) -> str:
        message = original_format(report)
        score = getattr(report, "spam_score", None)
        signals = getattr(report, "spam_signals", [])
        if score is None:
            score, signals = spam_signals(report)
        details = "؛ ".join(signals[:3]) if signals else "لم تظهر إشارات Spam قوية ضمن البيانات المتاحة"
        return f"🧹 مؤشر Spam heuristic: {score}/100\n{details}\n\n{message}"

    discovery.qualifies = score_candidate
    discovery.format_candidate = format_candidate


def with_buy_tips(message: str) -> str:
    return f"{message}\n\n{BUY_TIPS}"


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await core.scan_command(update, context)
    if update.message:
        await update.message.reply_text(BUY_TIPS, disable_web_page_preview=True)


async def spamfilter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    option = context.args[0].lower() if context.args else "status"
    if option not in {"on", "off", "status"}:
        await update.message.reply_text("الاستخدام: /spamfilter on أو /spamfilter off أو /spamfilter status")
        return
    if option == "status":
        enabled = spam_filter_enabled(user_id)
        await update.message.reply_text(
            f"فلتر Spam الشخصي {'مفعّل ✅' if enabled else 'متوقف ⏸️'}؛ عتبة الاستبعاد {SPAM_FILTER_THRESHOLD}/100. "
            "الفحص heuristic وقد يصنّف بعض التوكنات خطأ."
        )
        return
    enabled = option == "on"
    set_spam_filter(user_id, enabled)
    await update.message.reply_text(
        f"تم {'تفعيل' if enabled else 'إيقاف'} فلتر Spam لقائمتك. عتبة الاستبعاد {SPAM_FILTER_THRESHOLD}/100."
    )


async def buytips_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(BUY_TIPS, disable_web_page_preview=True)


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not update.effective_user:
        await update.message.reply_text("تعذر تحديد المستخدم.")
        return
    await update.message.reply_text("أفحص المصادر وأطبّق مؤشرات Spam…")
    try:
        reports, examined = await discovery.scan_discovery(
            context.bot_data["http_client"], include_seen=True
        )
        enabled = spam_filter_enabled(update.effective_user.id)
        if enabled:
            shown = [r for r in reports if getattr(r, "spam_score", spam_signals(r)[0]) < SPAM_FILTER_THRESHOLD]
            filtered = len(reports) - len(shown)
        else:
            shown = reports
            filtered = 0
        if not shown:
            await update.message.reply_text(
                f"لم يظهر مرشح بعد الفلاتر (فُحص {examined}؛ استُبعد {filtered} بفلتر Spam). جرّب لاحقًا."
            )
            return
        for report in sorted(shown, key=discovery.rank_key)[:5]:
            await update.message.reply_text(
                with_buy_tips(discovery.format_candidate(report)), disable_web_page_preview=True
            )
    except Exception as exc:
        logger.exception("Discovery failed")
        await update.message.reply_text(f"تعذر إكمال البحث: {exc}")


async def discover_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    recipients = discovery.subscribers()
    if not recipients:
        return
    try:
        async with __import__("httpx").AsyncClient(timeout=core.HTTP_TIMEOUT) as client:
            reports, _ = await discovery.scan_discovery(client)
        for user_id in recipients:
            enabled = spam_filter_enabled(user_id)
            reports_for_user = [
                report for report in reports
                if not enabled or getattr(report, "spam_score", spam_signals(report)[0]) < SPAM_FILTER_THRESHOLD
            ]
            for report in sorted(reports_for_user, key=discovery.rank_key)[:5]:
                try:
                    await context.bot.send_message(
                        user_id,
                        with_buy_tips(discovery.format_candidate(report)),
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logger.exception("Could not notify user %s", user_id)
    except Exception:
        logger.exception("Discovery job failed")


async def configure_command_menu(application: Application) -> None:
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
        BotCommand("spamfilter", "تفعيل/إيقاف فلتر Spam الشخصي"),
        BotCommand("buytips", "عرض قائمة تحقق بحثية عامة ومخاطر التداول"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_my_commands(commands, language_code="ar")
    logger.info("Telegram slash-command menu configured")


def main() -> None:
    if not core.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Add it as a Railway service variable and redeploy.")

    install_spam_scoring()
    core.init_db()
    discovery.init_db()
    init_preferences_db()
    app = Application.builder().token(core.BOT_TOKEN).post_init(configure_command_menu).build()

    handlers = [
        ("start", core.start_command),
        ("help", core.help_command),
        ("status", core.status_command),
        ("scan", scan_command),
        ("watch", core.watch_command),
        ("unwatch", core.unwatch_command),
        ("watchlist", core.watchlist_command),
        ("top", core.top_command),
        ("discover", discover_command),
        ("autodiscover", discovery.autodiscover_command),
        ("spamfilter", spamfilter_command),
        ("buytips", buytips_command),
    ]
    for name, callback in handlers:
        app.add_handler(CommandHandler(name, callback))

    if not app.job_queue:
        raise SystemExit("JobQueue unavailable; install the job-queue extra.")
    app.job_queue.run_repeating(
        discover_job,
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
        "Wolf524 starting; Birdeye enabled=%s; spam threshold=%s",
        bool(discovery.BIRDEYE_API_KEY), SPAM_FILTER_THRESHOLD,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
