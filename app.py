import logging

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler

import bot as core
import runner as discovery

logger = logging.getLogger("wolf524")


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
        BotCommand("discover", "البحث عن توكنات جديدة الآن"),
        BotCommand("autodiscover", "تشغيل/إيقاف تنبيهات الاكتشاف: on أو off"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_my_commands(commands, language_code="ar")
    logger.info("Telegram slash-command menu configured")


def main() -> None:
    if not core.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Add it as a Railway service variable and redeploy.")

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
    logger.info("Wolf524 analytics starting; Birdeye enabled=%s", bool(discovery.BIRDEYE_API_KEY))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
