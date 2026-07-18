"""Türkiye telefon numarası normalize ve eşleştirme."""

from __future__ import annotations

import re


def digits_only(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D+", "", str(value))


def normalize_tr_phone(raw: str | None) -> str | None:
    """
    Girişleri E.164 benzeri 90XXXXXXXXXX formuna çevirir.

    Desteklenen örnekler:
      905551112233, +90 555 111 22 33, 05551112233, 5551112233
    """
    d = digits_only(raw)
    if not d:
        return None

    # 0090... → 90...
    if d.startswith("0090") and len(d) >= 14:
        d = d[2:]

    if d.startswith("90") and len(d) >= 12:
        # 90 + 10 hane (5XXXXXXXXX)
        body = d[2:]
        if len(body) >= 10:
            return "90" + body[-10:]
        return None

    if d.startswith("0") and len(d) >= 11:
        body = d[1:]
        if len(body) >= 10:
            return "90" + body[-10:]
        return None

    # 10 hane: 5XXXXXXXXX
    if len(d) == 10 and d.startswith("5"):
        return "90" + d

    # 11 hane ve 0 ile başlamıyorsa son 10 haneyi dene
    if len(d) >= 10:
        last10 = d[-10:]
        if last10.startswith("5"):
            return "90" + last10

    return None


def phone_match_keys(raw: str | None) -> set[str]:
    """Karşılaştırma için olası anahtar seti (normalize + son 10 hane)."""
    keys: set[str] = set()
    d = digits_only(raw)
    if d:
        keys.add(d)
        if len(d) >= 10:
            keys.add(d[-10:])
    norm = normalize_tr_phone(raw)
    if norm:
        keys.add(norm)
        keys.add(norm[-10:])
    return {k for k in keys if k}


def phones_equal(a: str | None, b: str | None) -> bool:
    ka = phone_match_keys(a)
    kb = phone_match_keys(b)
    return bool(ka and kb and (ka & kb))
