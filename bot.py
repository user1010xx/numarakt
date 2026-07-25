"""
Toniva Görüşme Raporu kontrol botu.

Kullanım (yalnızca grup/supergroup):
  /kt 905551112233
  /ping  → sağlık kontrolü
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import Settings, load_settings
from phone_utils import normalize_tr_phone
from toniva_client import TonivaClient

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
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

    # Sadece grup (eski davranış) — ama özelde bilgi ver ki "ölü bot" sanılmasın
    if not _is_group(chat.type):
        await msg.reply_text("Bu bot yalnızca gruplarda /kt komutunu yanıtlar.")
        return

    if not _chat_allowed(settings, chat.id):
        logger.info("izin yok chat_id=%s", chat.id)
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
    me = await app.bot.get_me()
    logger.info("Bot hazır: @%s (id=%s)", me.username, me.id)


async def post_shutdown(app: Application) -> None:
    client: TonivaClient | None = app.bot_data.get("toniva")
    if client:
        await client.aclose()
        logger.info("Toniva client kapatıldı")


def main() -> None:
    settings = load_settings()

    # Cache isteğe bağlı — hata olursa bot yine ayağa kalksın
    cache = None
    try:
        from call_cache import CallCache

        cache = CallCache(settings.cache_path)
        logger.info("Cache: %s (%s satır)", settings.cache_path, cache.stats().row_count)
    except Exception:
        logger.exception("Cache açılamadı, önbelleksiz devam")

    client = TonivaClient(
        api_key=settings.toniva_api_key,
        base_url=settings.toniva_base_url,
        cache=cache,
    )

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
        "Başlatılıyor… lookback=%s gün, allowed_chats=%s",
        settings.lookback_days,
        sorted(settings.allowed_chat_ids) if settings.allowed_chat_ids else "tüm gruplar",
    )
    # Orijinal çalışan polling ayarları
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
