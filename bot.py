"""
Toniva /kt bot — yalnızca gruplarda.

  /kt 905551112233
  /ping
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings, load_settings
from phone_utils import normalize_tr_phone
from toniva_client import TonivaClient

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("kt-bot")

_KT_RE = re.compile(r"^/kt(?:@\w+)?\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _esc(t: str) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _lookback(settings: Settings):
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    return today - timedelta(days=settings.lookback_days), today


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sağlık kontrolü — her yerde yanıt verir."""
    if not update.effective_message:
        return
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"pong ✅ chat={chat.id if chat else '?'} type={chat.type if chat else '?'}"
    )


async def kt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        logger.warning("kt: message/chat yok")
        return

    logger.info(
        "kt alındı chat_id=%s type=%s user=%s text=%r",
        chat.id,
        chat.type,
        update.effective_user.id if update.effective_user else None,
        (msg.text or "")[:80],
    )

    # Hemen görünür yanıt (grup/özel fark etmez — sessiz kalma)
    try:
        wait = await msg.reply_text("🔍 Aranıyor…")
    except Exception:
        logger.exception("İlk reply başarısız")
        return

    try:
        settings: Settings = context.application.bot_data["settings"]
        client: TonivaClient = context.application.bot_data["toniva"]

        # Grup kısıtı
        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await wait.edit_text("Bu komut yalnızca gruplarda çalışır.")
            return

        if settings.allowed_chat_ids and chat.id not in settings.allowed_chat_ids:
            await wait.edit_text(
                f"Bu grup yetkili değil.\nchat_id: <code>{chat.id}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        args = context.args or []
        raw = " ".join(args).strip()
        if not raw and msg.text:
            m = _KT_RE.match(msg.text.strip())
            if m:
                raw = m.group(1).strip()

        if not raw:
            await wait.edit_text(
                "Kullanım: <code>/kt 905551112233</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        phone = normalize_tr_phone(raw)
        if not phone:
            await wait.edit_text(
                "Geçersiz numara. Örnek: <code>/kt 905551112233</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        await wait.edit_text(
            f"🔍 Aranıyor…\n<code>{_esc(phone)}</code>",
            parse_mode=ParseMode.HTML,
        )

        start, end = _lookback(settings)

        async def progress(text: str) -> None:
            try:
                await wait.edit_text(
                    f"🔍 {_esc(text)}\n<code>{_esc(phone)}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        result = await client.find_latest_call(
            phone, start, end, on_progress=progress, timeout_sec=20.0
        )

        if result.record is None:
            note = result.note or "kayıt yok"
            await wait.edit_text(
                f"❌ <b>BULUNAMADI</b>\n"
                f"Numara: <code>{_esc(phone)}</code>\n"
                f"Aralık: {start} → {end}\n"
                f"Not: {_esc(note)}",
                parse_mode=ParseMode.HTML,
            )
            return

        r = result.record
        src = f"\n🗄 {_esc(result.source)}" if result.source else ""
        await wait.edit_text(
            f"👤 <b>Personel:</b> {_esc(r.agent_name)}\n"
            f"📞 <b>Telefon:</b> {_esc(r.phone)}\n"
            f"📅 <b>Son arama tarihi:</b> {_esc(r.call_date)}\n"
            f"🕐 <b>Son arama saati:</b> {_esc(r.call_time)}"
            f"{src}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("kt hata")
        err = f"{type(exc).__name__}: {exc}"
        try:
            await wait.edit_text(f"⚠️ Hata: {_esc(err)[:500]}")
        except Exception:
            try:
                await msg.reply_text(f"⚠️ Hata: {err[:500]}")
            except Exception:
                pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("PTB error: %s", context.error, exc_info=context.error)


async def post_init(app: Application) -> None:
    try:
        me = await app.bot.get_me()
        logger.info("Bot online: @%s id=%s", me.username, me.id)
    except Exception:
        logger.exception("get_me başarısız — token kontrol edin")
        raise

    # Arka plan sync: varsayılan KAPALI (rate limit / takılma olmasın)
    settings: Settings = app.bot_data["settings"]
    client: TonivaClient = app.bot_data["toniva"]
    if settings.cache_sync_on_start and client.cache is not None:
        async def _sync() -> None:
            try:
                start, end = _lookback(settings)
                logger.info("Cache sync (son 2 gün)…")
                await client.sync_to_cache(start, end, max_days=2)
                logger.info("Cache sync bitti")
            except Exception:
                logger.exception("cache sync hata")

        app.bot_data["sync_task"] = asyncio.create_task(_sync())
    else:
        logger.info("Cache sync kapalı (CACHE_SYNC_ON_START=false)")


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("sync_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    client = app.bot_data.get("toniva")
    if client:
        await client.aclose()


def main() -> None:
    try:
        settings = load_settings()
    except SystemExit as e:
        logger.error("Ayar hatası: %s", e)
        raise
    except Exception:
        logger.exception("load_settings patladı")
        raise

    cache = None
    try:
        from call_cache import CallCache

        cache = CallCache(settings.cache_path)
        logger.info("Cache OK path=%s rows=%s", settings.cache_path, cache.stats().row_count)
    except Exception:
        logger.exception("Cache yok, devam (önbelleksiz)")

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
        .concurrent_updates(True)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["toniva"] = client
    app.bot_data["cache"] = cache

    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("kt", kt_cmd))
    # Privacy mode / mention fallback: "/kt@Bot 05..." text
    app.add_handler(
        MessageHandler(filters.Regex(r"(?i)^/kt(?:@\w+)?(?:\s|$)"), kt_cmd)
    )
    app.add_error_handler(on_error)

    logger.info(
        "Polling… lookback=%s allowed=%s",
        settings.lookback_days,
        sorted(settings.allowed_chat_ids) if settings.allowed_chat_ids else "ALL",
    )
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception:
        logger.error("run_polling crash:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
