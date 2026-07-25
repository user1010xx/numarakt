"""Sayfalama: total_count varken kısa sayfada erken kesilmemeli."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from toniva_client import TonivaClient


@pytest.mark.asyncio
async def test_paginate_until_total_count_despite_short_pages():
    """
    Canlı bug: pageSize=1000 istenir, API 200 döner, total=1000.
    Eski kod batch < pageSize deyip 200'de duruyordu.
    """
    client = TonivaClient(api_key="x")
    calls: list[int] = []

    async def fake_get(params):
        page = int(params.get("page") or 1)
        calls.append(page)
        start_idx = (page - 1) * 200
        rows = [
            {
                "Phone": f"905400000{str(i).zfill(3)}"[-12:].rjust(12, "0"),
                "ExtensionName": "a",
                "CreateDate": "2026-07-21",
                "CreateTime": "10:00:00",
                "CallID": f"id-{i}",
            }
            for i in range(start_idx, min(start_idx + 200, 1000))
        ]
        # Phone'ları geçerli TR mobil yap
        rows = []
        for i in range(start_idx, min(start_idx + 200, 1000)):
            # 905550000000 + i
            num = f"90555{i:07d}"
            rows.append(
                {
                    "Phone": num,
                    "ExtensionName": "a",
                    "CreateDate": "2026-07-21",
                    "CreateTime": "10:00:00",
                    "CallID": f"id-{i}",
                }
            )
        return {
            "meta": {"total_count": 1000, "page": page, "page_size": 200},
            "rows": rows,
        }

    client._get_report = fake_get  # type: ignore[method-assign]

    rows, meta = await client._fetch_window_paginated(
        date(2026, 7, 21), date(2026, 7, 21)
    )
    assert len(rows) == 1000
    assert meta["fetched_count"] == 1000
    assert len(calls) == 5  # 200*5
    assert calls == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_find_number_on_later_page():
    """Aranan numara 1. sayfada değil, total bitene kadar taranmalı."""
    client = TonivaClient(api_key="x")
    target = "905466033161"

    async def fake_get(params):
        page = int(params.get("page") or 1)
        rows = []
        for i in range(200):
            idx = (page - 1) * 200 + i
            phone = target if idx == 450 else f"90555{idx:07d}"
            rows.append(
                {
                    "Phone": phone,
                    "ExtensionName": "asu" if phone == target else "x",
                    "CreateDate": "2026-07-21",
                    "CreateTime": "13:23:22",
                    "CallTime": "00:01:06",
                    "CallID": f"id-{idx}",
                }
            )
        return {"meta": {"total_count": 600}, "rows": rows}

    client._get_report = fake_get  # type: ignore[method-assign]

    result = await client.find_latest_call(
        target, date(2026, 7, 21), date(2026, 7, 21)
    )
    assert result.record is not None
    assert result.record.phone == target
    assert result.record.agent_name == "asu"
    # idx=450 → 3. sayfa; erken çıkış — 600 satırın tamamı çekilmez
    assert result.row_count == 600  # 3*200
    assert result.meta_summary.get("early_exit") is True
    assert result.meta_summary.get("pages_scanned") == 3


def test_live_schema_row_parse():
    client = TonivaClient(api_key="x")
    row = {
        "Direction": "Dış Arama",
        "HangupSide": "—",
        "ExtensionName": "asu",
        "ExtensionNumber": "605",
        "Phone": "905466033161",
        "CreateDate": "2026-07-21",
        "CreateTime": "13:23:22",
        "RingTime": "00:00:07",
        "WaitTime": "00:00:00",
        "CallTime": "00:01:06",
        "CallID": "abc",
    }
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"
    assert rec.agent_name == "asu"
    assert rec.call_date == "21.07.2026"
    assert rec.call_time == "13:23:22"
    assert rec.talk_seconds == 66
