"""
Toniva Görüşme Raporu kontrol botu.

  /kt 905551112233
  /ping
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Railway log'larda hemen görünsün (buffer yok)
print("BOT_ENTRY: bot.py yüklendi", flush=True)

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

print("BOT_ENTRY: telegram import OK", flush=True)

from config import Settings, load_settings
from phone_utils import normalize_tr_phone
from toniva_client import TonivaClient

print("BOT_ENTRY: local import OK", flush=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("kt-bot")

_KT_RE = re.compile(r"^/kt(?:@\w+)?\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _is_group(chat_type: str) -> bool:
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


def _chat_allowed(settings: Settings, chat_id: int) -> bool:
    if not settings.allowed_chat_ids:
        return True
    return chat_id in settings.allowed_chat_ids


def _lookback_range(settings: Settings) -> tuple:
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    start = today - timedelta(days=settings.lookback_days)
    return start, today


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_found(record) -> str:
    return (
        f"👤 <b>Personel:</b> {_esc(record.agent_name)}\n"
        f"📞 <b>Telefon:</b> {_esc(record.phone)}\n"
        f"📅 <b>Son arama tarihi:</b> {_esc(record.call_date)}\n"
        f"🕐 <b>Son arama saati:</b> {_esc(record.call_time)}"
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    logger.info("/ping chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await update.effective_message.reply_text("pong ✅ bot çalışıyor")


async def kt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    settings: Settings = context.application.bot_data["settings"]
    client: TonivaClient = context.application.bot_data["toniva"]
    chat = update.effective_chat
    msg = update.effective_message

    logger.info(
        "/kt chat_id=%s type=%s text=%r",
        chat.id,
        chat.type,
        (msg.text or "")[:100],
    )

    if not _is_group(chat.type):
        await msg.reply_text("Bu bot yalnızca gruplarda /kt komutunu yanıtlar. /ping her yerde çalışır.")
        return

    if not _chat_allowed(settings, chat.id):
        logger.info("izin yok chat_id=%s allowed=%s", chat.id, settings.allowed_chat_ids)
        await msg.reply_text(
            f"Bu grup listede değil.\nchat_id: <code>{chat.id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []
    raw_phone = " ".join(args).strip() if args else ""
    if not raw_phone and msg.text:
        m = _KT_RE.match(msg.text.strip())
        if m:
            raw_phone = m.group(1).strip()

    if not raw_phone:
        await msg.reply_text(
            "Kullanım: <code>/kt 905551112233</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    normalized = normalize_tr_phone(raw_phone)
    if not normalized:
        await msg.reply_text(
            "Geçersiz numara.\nÖrnek: <code>/kt 905551112233</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    start, end = _lookback_range(settings)
    wait = await msg.reply_text(
        f"🔍 Son {settings.lookback_days} gün taranıyor…\n"
        f"<code>{_esc(normalized)}</code>",
        parse_mode=ParseMode.HTML,
    )

    try:
        result = await client.find_latest_call(normalized, start, end)
    except Exception as exc:
        logger.exception("Sorgulama hatası")
        await wait.edit_text(f"⚠️ Sorgulanamadı: {_esc(str(exc))[:400]}")
        return

    if result.record is None:
        await wait.edit_text(
            f"❌ <b>BULUNAMADI</b>\n"
            f"Numara: <code>{_esc(normalized)}</code>\n"
            f"Aralık: {start.isoformat()} → {end.isoformat()}\n"
            f"Not: {_esc(result.note or 'kayıt yok')}",
            parse_mode=ParseMode.HTML,
        )
        return

    await wait.edit_text(_format_found(result.record), parse_mode=ParseMode.HTML)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update hatası: %s", context.error, exc_info=context.error)


async def post_init(app: Application) -> None:
    print("BOT_ENTRY: post_init", flush=True)
    me = await app.bot.get_me()
    logger.info("Bot hazır: @%s (id=%s)", me.username, me.id)
    print(f"BOT_ENTRY: get_me OK @{me.username}", flush=True)


async def post_shutdown(app: Application) -> None:
    client: TonivaClient | None = app.bot_data.get("toniva")
    if client:
        await client.aclose()
        logger.info("Toniva client kapatıldı")


def main() -> None:
    print("BOT_ENTRY: main() başladı", flush=True)
    settings = load_settings()
    print(
        "BOT_ENTRY: settings OK "
        f"token_len={len(settings.telegram_bot_token)} "
        f"api_len={len(settings.toniva_api_key)} "
        f"lookback={settings.lookback_days} "
        f"allowed={len(settings.allowed_chat_ids) or 'ALL'}",
        flush=True,
    )

    cache = None
    try:
        from call_cache import CallCache

        cache = CallCache(settings.cache_path)
        logger.info(
            "Cache: %s (%s satır)", settings.cache_path, cache.stats().row_count
        )
    except Exception:
        logger.exception("Cache açılamadı, önbelleksiz devam")

    client = TonivaClient(
        api_key=settings.toniva_api_key,
        base_url=settings.toniva_base_url,
        cache=cache,
    )
    print("BOT_ENTRY: TonivaClient OK", flush=True)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["toniva"] = client

    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("kt", kt_command))
    app.add_error_handler(on_error)

    logger.info(
        "Polling başlıyor… lookback=%s allowed_chats=%s",
        settings.lookback_days,
        sorted(settings.allowed_chat_ids) if settings.allowed_chat_ids else "tüm gruplar",
    )
    print("BOT_ENTRY: run_polling()", flush=True)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(f"BOT_FATAL SystemExit: {e}", flush=True)
        raise
    except Exception:
        print("BOT_FATAL crash:", flush=True)
        traceback.print_exc()
        sys.exit(1)
