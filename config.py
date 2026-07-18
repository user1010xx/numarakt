"""Ortam değişkenlerinden uygulama ayarları."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _parse_chat_ids(raw: str | None) -> frozenset[int]:
    if not raw or not raw.strip():
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise SystemExit(
                f"ALLOWED_CHAT_IDS geçersiz değer: {part!r} (tam sayı olmalı)"
            ) from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    toniva_api_key: str
    toniva_base_url: str
    allowed_chat_ids: frozenset[int]
    timezone: str
    lookback_days: int = 30


def load_settings() -> Settings:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    api_key = (os.getenv("TONIVA_API_KEY") or "").strip()
    base_url = (
        os.getenv("TONIVA_BASE_URL") or "https://crm.toniva.net/api/public/v1"
    ).strip().rstrip("/")

    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN tanımlı değil.")
    if not api_key:
        raise SystemExit("TONIVA_API_KEY tanımlı değil.")

    return Settings(
        telegram_bot_token=token,
        toniva_api_key=api_key,
        toniva_base_url=base_url,
        allowed_chat_ids=_parse_chat_ids(os.getenv("ALLOWED_CHAT_IDS")),
        timezone=(os.getenv("TZ") or "Europe/Istanbul").strip(),
    )
