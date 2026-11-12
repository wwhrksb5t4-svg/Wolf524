import asyncio
import base64
import logging
import os
import re
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "wolf524.db")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
DEXSCREENER_API = "https://api.dexscreener.com"
SCAN_INTERVAL_SECONDS = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "300")))
HTTP_TIMEOUT = 20.0

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("wolf524")

# Valid base58 characters. We also check the decoded length to reject malformed mint addresses.
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_MAP = {char: index for index, char in enumerate(BASE58_ALPHABET)}


@dataclass
class TokenReport:
    mint: str
    name: str
    symbol: str
    pair_address: str | None
    price_usd: float | None
    liquidity_usd: float | None
    volume_24h: float | None
    buys_24h: int | None
    sells_24h: int | None
    pair_age_hours: float | None
    mint_authority: bool | None
    freeze_authority: bool | None
    top10_percent: float | None
    largest_accounts: list[tuple[str, float]]
    risk_score: int
    risk_flags: list[str]
    scanned_at: datetime


def init_db() -> None:
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS watchlist (
            user_id INTEGER NOT NULL, mint TEXT NOT NULL, added_at TEXT NOT NULL,
            PRIMARY KEY (user_id, mint))""")
        db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            mint TEXT NOT NULL, scanned_at TEXT NOT NULL, liquidity_usd REAL,
            risk_score INTEGER, PRIMARY KEY (mint, scanned_at))""")
        db.commit()


def validate_mint(mint: str) -> bool:
    if not mint or any(char not in BASE58_MAP for char in mint):
        return False
    number = 0
    for char in mint:
        number = number * 58 + BASE58_MAP[char]
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    decoded = b"\x00" * (len(mint) - len(mint.lstrip("1"))) + decoded
    return len(decoded) == 32


def short_address(address: str) -> str:
    return address if len(address) <= 12 else f"{address[:5]}…{address[-5:]}"


def usd(value: float | None) -> str:
    if value is None:
        return "غير متاح"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:,.4f}"


def age_text(hours: float | None) -> str:
    if hours is None:
        return "غير معروف (وقت إنشاء زوج التداول فقط)"
    if hours < 24:
        return f"{hours:.1f} ساعة"
    return f"{hours / 24:.1f} يوم"


async def rpc(client: httpx.AsyncClient, method: str, params: list[Any]) -> Any:
    response = await client.post(
        SOLANA_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"].get("message", "Solana RPC error")))
    return payload.get("result")


def decode_mint_authorities(account_data: str) -> tuple[bool, bool]:
    raw = base64.b64decode(account_data)
    if len(raw) < 82:
        raise ValueError("Invalid SPL token mint account")
    mint_option = struct.unpack_from("<I", raw, 0)[0]
    freeze_option = struct.unpack_from("<I", raw, 46)[0]
    return mint_option == 1, freeze_option == 1


async def fetch_dex_pair(client: httpx.AsyncClient, mint: str) -> dict[str, Any] | None:
    response = await client.get(f"{DEXSCREENER_API}/token-pairs/v1/solana/{mint}")
    response.raise_for_status()
    pairs = response.json()
    if not isinstance(pairs, list) or not pairs:
        return None
    sol_pairs = [pair for pair in pairs if pair.get("chainId") == "solana"]
    pairs = sol_pairs or pairs
    return max(pairs, key=lambda pair: float((pair.get("liquidity") or {}).get("usd") or 0))


async def scan_token(mint: str, client: httpx.AsyncClient) -> TokenReport:
    if not validate_mint(mint):
        raise ValueError("عنوان العملة غير صحيح؛ أدخل عنوان Mint صالحًا على Solana.")

    account_result, supply_result, pair_result = await asyncio.gather(
        rpc(client, "getAccountInfo", [mint, {"encoding": "base64", "commitment": "confirmed"}]),
        rpc(client, "getTokenSupply", [mint, {"commitment": "confirmed"}]),
        fetch_dex_pair(client, mint),
        return_exceptions=True,
    )
    if isinstance(account_result, Exception):
        raise RuntimeError(f"تعذر فحص حساب العملة عبر Solana RPC: {account_result}")
    if isinstance(supply_result, Exception):
        raise RuntimeError(f"تعذر قراءة المعروض عبر Solana RPC: {supply_result}")
    if isinstance(pair_result, Exception):
        logger.warning("DexScreener request failed for %s: %s", mint, pair_result)
        pair_result = None

    account = (account_result or {}).get("value")
    mint_authority: bool | None = None
    freeze_authority: bool | None = None
    if account and account.get("data"):
        try:
            mint_authority, freeze_authority = decode_mint_authorities(account["data"][0])
        except (ValueError, IndexError, struct.error):
            logger.info("Could not decode mint account for %s", mint)

    supply_value = (supply_result or {}).get("value") or {}
    raw_supply = float(supply_value.get("amount", 0) or 0)
    decimals = int(supply_value.get("decimals", 0) or 0)
    supply = raw_supply / (10 ** decimals) if decimals < 30 else 0

    largest_accounts: list[tuple[str, float]] = []
    top10_percent: float | None = None
    try:
        largest_result = await rpc(client, "getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        accounts = ((largest_result or {}).get("value") or [])
        largest_accounts = [
            (str(item.get("address", "")), float(item.get("uiAmount") or 0))
            for item in accounts[:10]
        ]
        if supply > 0:
            top10_percent = min(100.0, sum(amount for _, amount in largest_accounts) / supply * 100)
    except Exception as exc:
        logger.warning("Could not read largest accounts for %s: %s", mint, exc)

    pair = pair_result or {}
    info = pair.get("baseToken") or {}
    liquidity_value = (pair.get("liquidity") or {}).get("usd")
    volume_value = (pair.get("volume") or {}).get("h24")
    txns = (pair.get("txns") or {}).get("h24") or {}
    created_at = pair.get("pairCreatedAt")
    pair_age_hours = None
    if isinstance(created_at, (int, float)):
        pair_age_hours = max(0.0, (datetime.now(timezone.utc).timestamp() * 1000 - created_at) / 3_600_000)

    report = TokenReport(
        mint=mint,
        name=str(info.get("name") or "Unknown token"),
        symbol=str(info.get("symbol") or "???"),
        pair_address=pair.get("pairAddress"),
        price_usd=_float_or_none(pair.get("priceUsd")),
        liquidity_usd=_float_or_none(liquidity_value),
        volume_24h=_float_or_none(volume_value),
        buys_24h=_int_or_none(txns.get("buys")),
        sells_24h=_int_or_none(txns.get("sells")),
        pair_age_hours=pair_age_hours,
        mint_authority=mint_authority,
        freeze_authority=freeze_authority,
        top10_percent=top10_percent,
        largest_accounts=largest_accounts,
        risk_score=0,
        risk_flags=[],
        scanned_at=datetime.now(timezone.utc),
    )
    report.risk_score, report.risk_flags = score_risk(report)
    save_snapshot(report)
    return report


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def score_risk(report: TokenReport) -> tuple[int, list[str]]:
    score = 0
    flags: list[str] = []
    if report.mint_authority is True:
        score += 25
        flags.append("Mint authority فعّالة: يمكن إنشاء وحدات جديدة.")
    if report.freeze_authority is True:
        score += 25
        flags.append("Freeze authority فعّالة: يمكن تجميد حسابات الحيازة.")
    if report.mint_authority is None or report.freeze_authority is None:
        flags.append("تعذر التحقق من إحدى صلاحيات Mint/Freeze؛ البيانات غير مكتملة.")
    if report.liquidity_usd is None:
        flags.append("لا توجد بيانات سيولة على DexScreener.")
    elif report.liquidity_usd < 10_000:
        score += 25
        flags.append("السيولة منخفضة جدًا (أقل من $10K).")
    elif report.liquidity_usd < 50_000:
        score += 15
        flags.append("السيولة محدودة (أقل من $50K).")
    if report.top10_percent is not None:
        if report.top10_percent >= 50:
            score += 25
            flags.append(f"تركيز مرتفع: أكبر 10 حسابات تملك نحو {report.top10_percent:.1f}% من المعروض.")
        elif report.top10_percent >= 30:
            score += 18
            flags.append(f"تركيز ملحوظ: أكبر 10 حسابات تملك نحو {report.top10_percent:.1f}% من المعروض.")
        elif report.top10_percent >= 15:
            score += 10
            flags.append(f"أكبر 10 حسابات تملك نحو {report.top10_percent:.1f}% من المعروض.")
    else:
        flags.append("تعذر حساب تركّز أكبر الحسابات.")
    if report.pair_age_hours is not None:
        if report.pair_age_hours < 24:
            score += 15
            flags.append("زوج التداول حديث جدًا (أقل من 24 ساعة).")
        elif report.pair_age_hours < 168:
            score += 8
            flags.append("زوج التداول حديث (أقل من 7 أيام).")
    if report.buys_24h is not None and report.sells_24h is not None:
        trades = report.buys_24h + report.sells_24h
        if trades < 10:
            score += 10
            flags.append("نشاط تداول ضعيف جدًا خلال 24 ساعة.")
        elif report.sells_24h > max(10, report.buys_24h * 3):
            score += 8
            flags.append("عدد عمليات البيع أعلى كثيرًا من الشراء خلال 24 ساعة.")
    return min(100, score), flags


def save_snapshot(report: TokenReport) -> None:
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute(
            "INSERT OR REPLACE INTO snapshots (mint, scanned_at, liquidity_usd, risk_score) VALUES (?, ?, ?, ?)",
            (report.mint, report.scanned_at.isoformat(), report.liquidity_usd, report.risk_score),
        )
        db.commit()


def get_previous_snapshot(mint: str, before: str) -> tuple[float | None, int | None] | None:
    with sqlite3.connect(DATABASE_PATH) as db:
        row = db.execute(
            "SELECT liquidity_usd, risk_score FROM snapshots WHERE mint = ? AND scanned_at < ? ORDER BY scanned_at DESC LIMIT 1",
            (mint, before),
        ).fetchone()
    return row if row else None


def get_watchlist(user_id: int) -> list[str]:
    with sqlite3.connect(DATABASE_PATH) as db:
        rows = db.execute("SELECT mint FROM watchlist WHERE user_id = ? ORDER BY added_at", (user_id,)).fetchall()
    return [row[0] for row in rows]


def add_watch(user_id: int, mint: str) -> bool:
    with sqlite3.connect(DATABASE_PATH) as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, mint, added_at) VALUES (?, ?, ?)",
            (user_id, mint, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
        return cursor.rowcount > 0


def remove_watch(user_id: int, mint: str) -> bool:
    with sqlite3.connect(DATABASE_PATH) as db:
        cursor = db.execute("DELETE FROM watchlist WHERE user_id = ? AND mint = ?", (user_id, mint))
        db.commit()
        return cursor.rowcount > 0


def get_all_watched() -> dict[str, list[int]]:
    with sqlite3.connect(DATABASE_PATH) as db:
        rows = db.execute("SELECT user_id, mint FROM watchlist ORDER BY mint").fetchall()
    result: dict[str, list[int]] = {}
    for user_id, mint in rows:
        result.setdefault(mint, []).append(user_id)
    return result


def report_text(report: TokenReport) -> str:
    market_buyers = report.buys_24h if report.buys_24h is not None else "غير متاح"
    market_sellers = report.sells_24h if report.sells_24h is not None else "غير متاح"
    concentration = f"{report.top10_percent:.1f}%" if report.top10_percent is not None else "غير متاح"
    authority_mint = "فع��الة ⚠️" if report.mint_authority is True else "غير فعّالة ✅" if report.mint_authority is False else "غير معروف"
    authority_freeze = "فعّالة ⚠️" if report.freeze_authority is True else "غير فعّالة ✅" if report.freeze_authority is False else "غير معروف"
    if report.risk_score >= 70:
        level = "مرتفع جدًا 🔴"
    elif report.risk_score >= 40:
        level = "مرتفع 🟠"
    elif report.risk_score >= 20:
        level = "متوسط 🟡"
    else:
        level = "منخفض حسب المؤشرات المتاحة 🟢"

    flags = "\n".join(f"• {flag}" for flag in report.risk_flags) or "• لم تظهر مؤشرات ضمن الفحوصات المتاحة."
    holders = "\n".join(f"• `{short_address(address)}` — {amount:,.4g}" for address, amount in report.largest_accounts[:5])
    holders = holders or "غير متاح"
    links = (
        f"[DexScreener](https://dexscreener.com/solana/{report.mint}) · "
        f"[Solscan](https://solscan.io/token/{report.mint}) · "
        f"[فتح في Phantom](https://phantom.app/ul/browse/https%3A%2F%2Fdexscreener.com%2Fsolana%2F{report.mint})"
    )
    return (
        f"🔎 *{_escape_md(report.name)} ({_escape_md(report.symbol)})*\n"
        f"العنوان: `{report.mint}`\n\n"
        f"💵 السعر: {usd(report.price_usd)}\n"
        f"💧 السيولة: {usd(report.liquidity_usd)}\n"
        f"📊 حجم التداول 24س: {usd(report.volume_24h)}\n"
        f"👥 عمليات الشراء/البيع 24س: {market_buyers} / {market_sellers}\n"
        f"🕒 عمر زوج التداول: {age_text(report.pair_age_hours)}\n"
        f"🪙 Mint authority: {authority_mint}\n"
        f"🧊 Freeze authority: {authority_freeze}\n"
        f"🐋 أكبر 10 حسابات توكن: {concentration}\n"
        f"أكبر الحسابات (عناوين حسابات توكن، لا يلزم أن تكون محافظًا فريدة):\n{holders}\n\n"
        f"⚠️ *مؤشر المخاطر: {report.risk_score}/100 — {level}*\n{flags}\n\n"
        f"{links}\n\n"
        "تنبيه: هذا فحص آلي تقريبي وليس نصيحة استثمارية أو ضمانًا ضد الاحتيال. عمر الزوج ليس بالضرورة عمر العملة، وبيانات أكبر الحسابات لا تثبت هوية المالك."
    )


def _escape_md(text: str) -> str:
    return re.sub(r"([_*`\[\]])", r"\\\1", text)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("استخدم: /scan <عنوان_Mint>")
        return
    mint = context.args[0].strip()
    await update.message.reply_text("جارٍ فحص العملة…")
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            report = await scan_token(mint, client)
        previous = get_previous_snapshot(mint, report.scanned_at.isoformat())
        # Previous snapshot is queried after insertion; the query excludes the current timestamp.
        await update.message.reply_text(report_text(report), parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as exc:
        logger.exception("Scan failed for %s", mint)
        await update.message.reply_text(f"تعذر إكمال الفحص: {exc}")


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not context.args:
        await update.message.reply_text("استخدم: /watch <عنوان_Mint>")
        return
    mint = context.args[0].strip()
    if not validate_mint(mint):
        await update.message.reply_text("عنوان Mint غير صالح على Solana.")
        return
    added = add_watch(update.effective_user.id, mint)
    await update.message.reply_text("أُضيفت العملة لقائمة المراقبة ✅" if added else "العملة موجودة بالفعل في قائمتك.")


async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not context.args:
        await update.message.reply_text("استخدم: /unwatch <عنوان_Mint>")
        return
    mint = context.args[0].strip()
    removed = remove_watch(update.effective_user.id, mint)
    await update.message.reply_text("حُذفت العملة من قائمتك." if removed else "العملة غير موجودة في قائمتك.")


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    mints = get_watchlist(update.effective_user.id)
    if not mints:
        await update.message.reply_text("قائمة المراقبة فارغة. أضف عملة باستخدام /watch <عنوان_Mint>.")
        return
    text = "📋 *قائمة مراقبتك:*\n" + "\n".join(f"• `{mint}`" for mint in mints)
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    mints = get_watchlist(update.effective_user.id)
    if not mints:
        await update.message.reply_text("قائمة المراقبة فارغة. أضف عملات باستخدام /watch <عنوان_Mint>.")
        return
    await update.message.reply_text(f"جارٍ ترتيب {len(mints)} عملة حسب مؤشر المخاطر (الأقل أولًا)…")
    reports: list[TokenReport] = []
    failures = 0
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for mint in mints[:20]:
            try:
                reports.append(await scan_token(mint, client))
                await asyncio.sleep(0.2)
            except Exception as exc:
                logger.warning("Could not rank %s: %s", mint, exc)
                failures += 1
    reports.sort(key=lambda item: (item.risk_score, -(item.liquidity_usd or 0)))
    if not reports:
        await update.message.reply_text("تعذر فحص العملات في قائمتك. تحقق من العناوين أو حاول لاحقًا.")
        return
    lines = [f"{index}. *{_escape_md(item.symbol)}* — المخاطر {item.risk_score}/100 · السيولة {usd(item.liquidity_usd)}\n`{item.mint}`" for index, item in enumerate(reports, 1)]
    suffix = f"\nتعذر فحص {failures} عملة." if failures else ""
    await update.message.reply_text("🏆 *الترتيب حسب انخفاض المخاطر:*\n" + "\n".join(lines) + suffix, parse_mode="Markdown", disable_web_page_preview=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "مرحبًا بك في Wolf524 🔎\n"
            "أرسل /scan <عنوان_Mint> لفحص توكن Solana.\n\n"
            "/watch <Mint> — إضافة لقائمة المراقبة\n"
            "/unwatch <Mint> — إزالة من القائمة\n"
            "/watchlist — عرض قائمتك\n"
            "/top — ترتيب عملات قائمتك حسب المخاطر\n"
            "/help — المساعدة"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "الأوامر:\n"
            "/scan <Mint> — الحجم والسيولة والشراء/البيع وعمر زوج التداول والتوزيع والصلاحيات ومؤشر المخاطر\n"
            "/watch <Mint> — مراقبة عملة وتنبيه عند هبوط السيولة الكبير\n"
            "/unwatch <Mint> — إيقاف مراقبتها\n"
            "/watchlist — عرض العملات المراقبة\n"
            "/top — ترتيب العملات المراقبة حسب أقل مؤشر مخاطر\n\n"
            "يدعم عناوين Solana SPL فقط. المؤشرات تقريبية ولا تضمن كشف الاحتيال."
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("البوت يعمل ✅")


async def monitor_watched(context: ContextTypes.DEFAULT_TYPE) -> None:
    watched = get_all_watched()
    if not watched:
        return
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for mint, user_ids in watched.items():
            try:
                # Compare with last saved scan before this scan; scan_token persists the new snapshot.
                old = get_previous_snapshot(mint, datetime.now(timezone.utc).isoformat())
                report = await scan_token(mint, client)
                if old and old[0] and report.liquidity_usd is not None and old[0] > 0:
                    decline = (old[0] - report.liquidity_usd) / old[0]
                    if decline >= 0.20:
                        message = (
                            f"🚨 *تنبيه هبوط السيولة*\n"
                            f"`{mint}`\n"
                            f"انخفضت السيولة {decline * 100:.1f}% منذ آخر فحص "
                            f"({usd(old[0])} ← {usd(report.liquidity_usd)}).\n\n"
                            f"[DexScreener](https://dexscreener.com/solana/{mint}) · "
                            f"[Solscan](https://solscan.io/token/{mint})"
                        )
                        for user_id in user_ids:
                            try:
                                await context.bot.send_message(user_id, message, parse_mode="Markdown", disable_web_page_preview=True)
                            except Exception:
                                logger.exception("Could not send alert to user %s", user_id)
                await asyncio.sleep(0.2)
            except Exception:
                logger.exception("Watch scan failed for %s", mint)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and set your Telegram bot token.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("unwatch", unwatch_command))
    app.add_handler(CommandHandler("watchlist", watchlist_command))
    app.add_handler(CommandHandler("top", top_command))
    if app.job_queue:
        app.job_queue.run_repeating(monitor_watched, interval=SCAN_INTERVAL_SECONDS, first=SCAN_INTERVAL_SECONDS, name="watchlist-monitor")
    else:
        raise SystemExit("JobQueue not installed. Install requirements.txt including the job-queue extra.")
    logger.info("Wolf524 Telegram bot is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
