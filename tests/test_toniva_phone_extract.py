"""Toniva satırından dış telefon seçimi — canlı BULUNAMADI hatasının regresyonu."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from phone_utils import phones_equal
from toniva_client import TonivaClient


@pytest.fixture
def client() -> TonivaClient:
    return TonivaClient(api_key="test-key")


def test_ui_style_row_parses(client: TonivaClient):
    """Panel kolon adlarıyla (TELEFON, DAHİLİ ADI, …)."""
    row = {
        "YÖN": "Dış Arama",
        "KAPATAN": "—",
        "DAHİLİ ADI": "asu",
        "DAHİLİ NUMARASI": "605",
        "TELEFON": "905466033161",
        "TARİH": "Salı 21 Temmuz 2026",
        "SAAT": "13:23:22",
        "ÇALDIRMA SÜRESİ": "00:00:07",
        "BEKLEME SÜRESİ": "00:00:00",
        "GÖRÜŞME SÜRESİ": "00:01:06",
    }
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"
    assert rec.agent_name == "asu"
    assert rec.call_date == "21.07.2026"
    assert rec.call_time == "13:23:22"
    assert rec.talk_seconds == 66
    assert phones_equal(rec.phone, "905466033161")


def test_phone_field_is_extension_dst_is_external(client: TonivaClient):
    """Eski bug: phone=605, dst=9054... → 605 seçiliyordu."""
    row = {
        "phone": "605",
        "dst": "905466033161",
        "src": "605",
        "cnam": "asu",
        "calldate": "2026-07-21 13:23:22",
        "billsec": 66,
    }
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"
    assert phones_equal(rec.phone, "905466033161")


def test_number_field_is_extension_external_number_wins(client: TonivaClient):
    row = {
        "number": "605",
        "externalNumber": "905466033161",
        "agentName": "asu",
        "calldate": "2026-07-21T13:23:22",
    }
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"


def test_src_dst_outbound(client: TonivaClient):
    row = {"src": "605", "dst": "905466033161", "cnam": "asu"}
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"


def test_src_dst_inbound(client: TonivaClient):
    """Gelen arama: src dış, dst dahili."""
    row = {"src": "905466033161", "dst": "605", "cnam": "asu"}
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"


def test_telefon_numarasi_field_name(client: TonivaClient):
    row = {"Telefon Numarası": "05466033161", "Dahili Adı": "asu"}
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"


def test_columns_as_objects_with_list_rows(client: TonivaClient):
    data = {
        "meta": {
            "columns": [
                {"key": "direction", "label": "YÖN"},
                {"key": "agent", "label": "DAHİLİ ADI"},
                {"key": "ext", "label": "DAHİLİ NUMARASI"},
                {"key": "phone", "label": "TELEFON"},
                {"key": "date", "label": "TARİH"},
                {"key": "time", "label": "SAAT"},
            ],
        },
        "rows": [
            [
                "Dış Arama",
                "asu",
                "605",
                "905466033161",
                "2026-07-21",
                "13:23:22",
            ]
        ],
    }
    rows = TonivaClient._extract_rows(data)
    assert len(rows) == 1
    # key tercih edilir → phone alanı
    assert rows[0].get("phone") == "905466033161"
    rec = client._parse_row(client._flatten_row(rows[0]))
    assert rec is not None
    assert rec.phone == "905466033161"


def test_find_latest_among_matches(client: TonivaClient, monkeypatch: pytest.MonkeyPatch):
    """Aynı sayfada birden fazla arama → en son tarih."""

    async def fake_get(params):
        return {
            "meta": {"total_count": 3},
            "rows": [
                {
                    "TELEFON": "905466033161",
                    "DAHİLİ ADI": "eski",
                    "TARİH": "2026-07-10",
                    "SAAT": "10:00:00",
                    "GÖRÜŞME SÜRESİ": "00:00:30",
                },
                {
                    "TELEFON": "905466033161",
                    "DAHİLİ ADI": "asu",
                    "TARİH": "Salı 21 Temmuz 2026",
                    "SAAT": "13:23:22",
                    "GÖRÜŞME SÜRESİ": "00:01:06",
                },
                {
                    "TELEFON": "905551112233",
                    "DAHİLİ ADI": "diger",
                    "TARİH": "2026-07-22",
                    "SAAT": "09:00:00",
                },
            ],
        }

    monkeypatch.setattr(client, "_get_report", fake_get)

    import asyncio

    result = asyncio.run(
        client.find_latest_call(
            "905466033161",
            date(2026, 6, 25),
            date(2026, 7, 25),
        )
    )
    assert result.record is not None
    assert result.record.agent_name == "asu"
    assert result.record.call_date == "21.07.2026"
    assert result.record.call_time == "13:23:22"
    assert result.match_count >= 1
    assert result.meta_summary.get("early_exit") is True


def test_find_latest_miss_when_only_other_numbers(
    client: TonivaClient, monkeypatch: pytest.MonkeyPatch
):
    async def fake_get(params):
        return {
            "meta": {"total_count": 1},
            "rows": [
                {
                    "phone": "605",
                    "dst": "905551112233",
                    "cnam": "x",
                    "calldate": "2026-07-21 12:00:00",
                }
            ],
        }

    monkeypatch.setattr(client, "_get_report", fake_get)

    import asyncio

    result = asyncio.run(
        client.find_latest_call(
            "905466033161",
            date(2026, 6, 25),
            date(2026, 7, 25),
        )
    )
    assert result.record is None
    assert result.row_count == 1


def test_regression_user_case_extension_shadowing(
    client: TonivaClient, monkeypatch: pytest.MonkeyPatch
):
    """Kullanıcı senaryosu: Dış Arama asu/605 → 905466033161."""

    async def fake_get(params):
        return {
            "meta": {"total_count": 1},
            "rows": [
                {
                    "YÖN": "Dış Arama",
                    "phone": "605",
                    "number": "605",
                    "src": "605",
                    "dst": "905466033161",
                    "TELEFON": "905466033161",
                    "DAHİLİ ADI": "asu",
                    "DAHİLİ NUMARASI": "605",
                    "TARİH": "Salı 21 Temmuz 2026",
                    "SAAT": "13:23:22",
                    "GÖRÜŞME SÜRESİ": "00:01:06",
                    "ÇALDIRMA SÜRESİ": "00:00:07",
                }
            ],
        }

    monkeypatch.setattr(client, "_get_report", fake_get)

    import asyncio

    result = asyncio.run(
        client.find_latest_call(
            "905466033161",
            date(2026, 6, 25),
            date(2026, 7, 25),
        )
    )
    assert result.record is not None
    assert result.record.agent_name == "asu"
    assert result.record.phone == "905466033161"
    assert result.record.sort_key == datetime(2026, 7, 21, 13, 23, 22)


def test_match_phone_in_unknown_field(client: TonivaClient, monkeypatch: pytest.MonkeyPatch):
    """Alan adı bilinmese bile değer taraması ile bulunur."""

    async def fake_get(params):
        return {
            "meta": {"total_count": 1},
            "rows": [
                {
                    "weirdField": "905466033161",
                    "agentLabel": "asu",
                    "when": "2026-07-21 13:23:22",
                }
            ],
        }

    monkeypatch.setattr(client, "_get_report", fake_get)

    import asyncio

    result = asyncio.run(
        client.find_latest_call(
            "905466033161",
            date(2026, 6, 25),
            date(2026, 7, 25),
        )
    )
    assert result.record is not None
    assert result.record.phone == "905466033161"


def test_deep_extract_rows_nested_payload():
    data = {
        "status": "ok",
        "payload": {
            "nested": {
                "rows": [
                    {"TELEFON": "905466033161", "DAHİLİ ADI": "asu"},
                ]
            }
        },
    }
    rows = TonivaClient._extract_rows(data)
    assert len(rows) == 1
    assert rows[0]["TELEFON"] == "905466033161"


def test_date_chunks():
    chunks = TonivaClient._date_chunks(date(2026, 6, 25), date(2026, 7, 25), max_days=14)
    assert chunks[0][0] == date(2026, 6, 25)
    assert chunks[-1][1] == date(2026, 7, 25)
    # örtüşmesiz
    for i in range(len(chunks) - 1):
        assert chunks[i][1] < chunks[i + 1][0]
