"""
Toniva Görüşme Raporu kontrol botu.

Kullanım (yalnızca grup/supergroup):
  /kt 905551112233
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from call_cache import CallCache
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


def _format_found(record, *, source: str = "") -> str:
    src = f"\n🗄 <i>{_esc(source)}</i>" if source else ""
    return (
        f"👤 <b>Personel:</b> {_esc(record.agent_name)}\n"
        f"📞 <b>Telefon:</b> {_esc(record.phone)}\n"
        f"📅 <b>Son arama tarihi:</b> {_esc(record.call_date)}\n"
        f"🕐 <b>Son arama saati:</b> {_esc(record.call_time)}"
        f"{src}"
    )


async def kt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Her zaman önce kullanıcıya görünür yanıt ver; sonra ara."""
    try:
        await _kt_command_impl(update, context)
    except Exception:
        logger.exception("kt_command beklenmeyen hata")
        try:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Beklenmeyen hata. Loglara bakın / tekrar deneyin."
                )
        except Exception:
            pass


async def _kt_command_impl(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    client: TonivaClient = context.application.bot_data["toniva"]

    if not update.effective_chat or not update.effective_message:
        return

    chat = update.effective_chat
    msg = update.effective_message

    if not _is_group(chat.type):
        # Sessiz kalma — kullanıcı "yanıt yok" sanmasın
        await msg.reply_text("Bu bot yalnızca grup sohbetlerinde çalışır. /kt …")
        return

    if not _chat_allowed(settings, chat.id):
        logger.info("İzin listesinde olmayan grup chat_id=%s", chat.id)
        await msg.reply_text("Bu grup için bot yetkili değil.")
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

    # Hemen yanıt — kullanıcı boş beklememeli
    wait = await msg.reply_text(
        f"🔍 Aranıyor…\n<code>{_esc(normalized)}</code>",
        parse_mode=ParseMode.HTML,
    )

    async def on_progress(text: str) -> None:
        try:
            await wait.edit_text(
                f"🔍 {_esc(text)}\n<code>{_esc(normalized)}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    try:
        result = await client.find_latest_call(
            normalized,
            start,
            end,
            on_progress=on_progress,
            timeout_sec=25.0,
        )
    except Exception as exc:
        logger.exception("Sorgulama hatası")
        await wait.edit_text(f"⚠️ Sorgulanamadı: {_esc(str(exc))}")
        return

    if result.record is None:
        lines = [
            "❌ <b>BULUNAMADI</b>",
            f"Numara: <code>{_esc(normalized)}</code>",
            f"Aralık: {start.isoformat()} → {end.isoformat()}",
        ]
        if result.note:
            lines.append(f"Not: {_esc(result.note)}")
        if result.source:
            lines.append(f"Kaynak: {_esc(result.source)}")
        cache: CallCache | None = context.application.bot_data.get("cache")
        if cache is not None:
            try:
                st = cache.stats()
                lines.append(f"Önbellek: {st.row_count} kayıt")
            except Exception:
                pass
        await wait.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    await wait.edit_text(
        _format_found(result.record, source=result.source or result.note),
        parse_mode=ParseMode.HTML,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update hatası: %s", context.error, exc_info=context.error)


async def post_init(app: Application) -> None:
    me = await app.bot.get_me()
    logger.info("Bot hazır: @%s (id=%s)", me.username, me.id)

    settings: Settings = app.bot_data["settings"]
    client: TonivaClient = app.bot_data["toniva"]
    cache: CallCache | None = app.bot_data.get("cache")

    if cache is None or not settings.cache_sync_on_start:
        logger.info("Cache sync kapalı veya cache yok")
        return

    async def _bg_sync() -> None:
        try:
            # Önce son 3 gün (hızlı ısınma), sonra kalan lookback
            start, end = _lookback_range(settings)
            logger.info("Cache sync faz1: son 3 gün")
            await client.sync_to_cache(start, end, max_days=3)
            logger.info("Cache sync faz2: tam aralık (yavaş)")
            await client.sync_to_cache(start, end)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Arka plan cache sync hata")

    app.bot_data["sync_task"] = asyncio.create_task(_bg_sync())


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("sync_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    client: TonivaClient | None = app.bot_data.get("toniva")
    if client:
        await client.aclose()
        logger.info("Toniva client kapatıldı")


def main() -> None:
    settings = load_settings()

    cache: CallCache | None = None
    try:
        cache = CallCache(settings.cache_path)
        logger.info("Cache açıldı: %s rows=%s", settings.cache_path, cache.stats().row_count)
    except Exception:
        logger.exception(
            "Cache açılamadı (%s) — önbelleksiz devam", settings.cache_path
        )
        cache = None

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
    app.bot_data["cache"] = cache

    app.add_handler(CommandHandler("kt", kt_command))
    app.add_error_handler(on_error)

    logger.info(
        "Polling başlıyor lookback=%s cache=%s",
        settings.lookback_days,
        "on" if cache else "off",
    )
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
