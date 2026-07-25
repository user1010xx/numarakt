"""Cache + hızlı arama stratejisi testleri."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from call_cache import CallCache
from toniva_client import CallRecord, TonivaClient


@pytest.fixture
def cache(tmp_path: Path) -> CallCache:
    return CallCache(tmp_path / "t.db")


def test_cache_find_latest(cache: CallCache):
    cache.upsert_records(
        [
            CallRecord(
                agent_name="eski",
                phone="905466033161",
                call_date="10.07.2026",
                call_time="10:00:00",
                talk_seconds=10,
                sort_key=datetime(2026, 7, 10, 10, 0, 0),
            ),
            CallRecord(
                agent_name="asu",
                phone="905466033161",
                call_date="21.07.2026",
                call_time="13:23:22",
                talk_seconds=66,
                sort_key=datetime(2026, 7, 21, 13, 23, 22),
            ),
        ]
    )
    rec = cache.find_latest("05466033161")
    assert rec is not None
    assert rec.agent_name == "asu"
    assert rec.call_time == "13:23:22"


def test_live_schema_parse():
    client = TonivaClient(api_key="x")
    row = {
        "Direction": "Dış Arama",
        "ExtensionName": "asu",
        "ExtensionNumber": "605",
        "Phone": "905466033161",
        "CreateDate": "2026-07-21",
        "CreateTime": "13:23:22",
        "RingTime": "00:00:07",
        "CallTime": "00:01:06",
        "CallID": "abc",
    }
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"
    assert rec.agent_name == "asu"
    assert rec.call_time == "13:23:22"
    assert rec.talk_seconds == 66


@pytest.mark.asyncio
async def test_find_uses_cache_first(cache: CallCache):
    cache.upsert_records(
        [
            CallRecord(
                agent_name="asu",
                phone="905466033161",
                call_date="21.07.2026",
                call_time="13:23:22",
                talk_seconds=66,
                sort_key=datetime(2026, 7, 21, 13, 23, 22),
            )
        ]
    )
    client = TonivaClient(api_key="x", cache=cache)

    async def boom(params):
        raise AssertionError("cache hit iken API çağrılmamalı")

    client._get_report = boom  # type: ignore

    result = await client.find_latest_call(
        "905466033161", date(2026, 6, 25), date(2026, 7, 25)
    )
    assert result.record is not None
    assert result.source == "cache"
    assert result.record.agent_name == "asu"


@pytest.mark.asyncio
async def test_day_scan_early_exit():
    client = TonivaClient(api_key="x")
    scan_pages: dict[str, int] = {}

    async def fake_get(params):
        day = params["startDate"]
        page = int(params.get("page") or 1)
        if params.get("startDate") == params.get("endDate"):
            scan_pages[day] = scan_pages.get(day, 0) + 1

        if day == "2026-07-21" and page == 2:
            return {
                "meta": {"total_count": 250},
                "rows": [
                    {
                        "Phone": "905466033161",
                        "ExtensionName": "asu",
                        "CreateDate": "2026-07-21",
                        "CreateTime": "13:23:22",
                        "CallID": f"hit-{page}",
                    }
                ],
            }
        rows = [
            {
                "Phone": f"90555{page:07d}",
                "ExtensionName": "x",
                "CreateDate": day,
                "CreateTime": "10:00:00",
                "CallID": f"{day}-{page}-{i}",
            }
            for i in range(200)
        ]
        return {"meta": {"total_count": 250}, "rows": rows}

    client._get_report = fake_get  # type: ignore

    result = await client.find_latest_call(
        "905466033161", date(2026, 7, 20), date(2026, 7, 22)
    )
    assert result.record is not None
    assert result.record.agent_name == "asu"
    assert result.source == "scan"
    assert "2026-07-20" not in scan_pages
    assert scan_pages.get("2026-07-21", 0) == 2


def test_extension_not_preferred_over_dst():
    client = TonivaClient(api_key="x")
    row = {"phone": "605", "dst": "905466033161", "cnam": "asu"}
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"
