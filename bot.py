"""
Toniva /kt bot.

  /ping  — her yerde
  /kt    — gruplarda
"""

from __future__ import annotations

import sys
import traceback

# En başta — import bile patlarsa bunu görürüz
print("BOOT 0: start", flush=True)

import logging
import re
from datetime import datetime, timedelta

print("BOOT 1: stdlib ok", flush=True)

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

print("BOOT 2: telegram ok", flush=True)

from config import load_settings
from phone_utils import normalize_tr_phone
from toniva_client import TonivaClient

print("BOOT 3: app modules ok", flush=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("kt-bot")

_KT_RE = re.compile(r"^/kt(?:@\w+)?\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _lookback_range(settings):
    # ZoneInfo bazen ortamda patlar — güvenli fallback
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(settings.timezone)
        today = datetime.now(tz).date()
    except Exception as exc:
        logger.warning("ZoneInfo yok (%s), UTC kullanılıyor", exc)
        today = datetime.utcnow().date()
    start = today - timedelta(days=settings.lookback_days)
    return start, today


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
    chat = update.effective_chat
    logger.info("/ping chat_id=%s type=%s", getattr(chat, "id", None), getattr(chat, "type", None))
    await update.effective_message.reply_text("pong ✅ bot çalışıyor")


async def kt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    settings = context.application.bot_data["settings"]
    client: TonivaClient = context.application.bot_data["toniva"]
    chat = update.effective_chat
    msg = update.effective_message

    logger.info("/kt chat_id=%s type=%s text=%r", chat.id, chat.type, (msg.text or "")[:100])

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text(" /kt yalnızca grupta çalışır. /ping her yerde çalışır.")
        return

    if settings.allowed_chat_ids and chat.id not in settings.allowed_chat_ids:
        await msg.reply_text(
            f"Bu grup yetkili değil.\nchat_id: <code>{chat.id}</code>\n"
            f"ALLOWED_CHAT_IDS içine bu id'yi ekleyin.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []
    raw = " ".join(args).strip() if args else ""
    if not raw and msg.text:
        m = _KT_RE.match(msg.text.strip())
        if m:
            raw = m.group(1).strip()

    if not raw:
        await msg.reply_text("Kullanım: <code>/kt 905551112233</code>", parse_mode=ParseMode.HTML)
        return

    phone = normalize_tr_phone(raw)
    if not phone:
        await msg.reply_text("Geçersiz numara. Örnek: <code>/kt 905551112233</code>", parse_mode=ParseMode.HTML)
        return

    start, end = _lookback_range(settings)
    wait = await msg.reply_text(
        f"🔍 Sorgulanıyor…\n<code>{_esc(phone)}</code>",
        parse_mode=ParseMode.HTML,
    )

    async def progress(text: str) -> None:
        try:
            await wait.edit_text(
                f"🔍 {_esc(text)}\n<code>{_esc(phone)}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    try:
        result = await client.find_latest_call(
            phone, start, end, on_progress=progress, timeout_sec=120.0
        )
    except Exception as exc:
        logger.exception("sorgu hatası")
        await wait.edit_text(f"⚠️ Sorgulanamadı: {_esc(str(exc))[:400]}")
        return

    if result.record is None:
        title = "⏱ TARAMA BİTMEDİ" if result.source == "timeout" else "❌ BULUNAMADI"
        await wait.edit_text(
            f"<b>{title}</b>\n"
            f"Numara: <code>{_esc(phone)}</code>\n"
            f"Aralık: {start} → {end}\n"
            f"Not: {_esc(result.note or 'kayıt yok')}",
            parse_mode=ParseMode.HTML,
        )
        return

    await wait.edit_text(_format_found(result.record), parse_mode=ParseMode.HTML)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("PTB error: %s", context.error, exc_info=context.error)


async def post_init(app: Application) -> None:
    print("BOOT 8: post_init / get_me", flush=True)
    me = await app.bot.get_me()
    print(f"BOOT 9: bot online @{me.username} id={me.id}", flush=True)
    logger.info("Bot hazır: @%s id=%s", me.username, me.id)


async def post_shutdown(app: Application) -> None:
    client = app.bot_data.get("toniva")
    if client:
        await client.aclose()


def main() -> None:
    print("BOOT 4: main()", flush=True)

    settings = load_settings()
    print(
        f"BOOT 5: settings token_len={len(settings.telegram_bot_token)} "
        f"api_len={len(settings.toniva_api_key)} "
        f"lookback={settings.lookback_days} "
        f"allowed={sorted(settings.allowed_chat_ids) if settings.allowed_chat_ids else 'ALL'}",
        flush=True,
    )

    # Cache bilerek KAPALI — önceki deploy settings OK sonrası burada takılıyordu
    print("BOOT 6: TonivaClient (cache=None)", flush=True)
    client = TonivaClient(
        api_key=settings.toniva_api_key,
        base_url=settings.toniva_base_url,
        cache=None,
    )
    print("BOOT 6b: client ok", flush=True)

    print("BOOT 7: Application build", flush=True)
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
    print("BOOT 7b: handlers ok → run_polling", flush=True)

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(f"BOOT FATAL SystemExit: {e}", flush=True)
        raise
    except BaseException:
        print("BOOT FATAL:", flush=True)
        traceback.print_exc()
        sys.exit(1)
