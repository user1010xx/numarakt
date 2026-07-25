"""Arama stratejisi testleri."""

from __future__ import annotations

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
    assert rec.talk_seconds == 66


@pytest.mark.asyncio
async def test_direct_phone_query_hit():
    client = TonivaClient(api_key="x")

    async def fake_get(params):
        if params.get("phone") == "905515395755":
            return {
                "meta": {"total_count": 1},
                "rows": [
                    {
                        "Phone": "905515395755",
                        "ExtensionName": "dilara",
                        "CreateDate": "2026-07-20",
                        "CreateTime": "11:00:00",
                        "CallTime": "00:00:30",
                        "CallID": "1",
                    }
                ],
            }
        return {"meta": {"total_count": 50000}, "rows": [{"Phone": "905400000000", "CallID": "x"}] * 30}

    client._get_report = fake_get  # type: ignore
    result = await client.find_latest_call(
        "905515395755", date(2026, 6, 25), date(2026, 7, 25)
    )
    assert result.record is not None
    assert result.source == "phone_query"
    assert result.record.agent_name == "dilara"


@pytest.mark.asyncio
async def test_stream_early_exit_on_later_page():
    client = TonivaClient(api_key="x")
    client._phone_param = False  # filtre atla
    pages = []

    async def fake_get(params):
        page = int(params.get("page") or 1)
        pages.append(page)
        if page == 3:
            return {
                "meta": {"total_count": 600},
                "rows": [
                    {
                        "Phone": "905466033161",
                        "ExtensionName": "asu",
                        "CreateDate": "2026-07-21",
                        "CreateTime": "13:23:22",
                        "CallID": "hit",
                    }
                ]
                + [
                    {
                        "Phone": f"90555{i:07d}",
                        "ExtensionName": "x",
                        "CreateDate": "2026-07-21",
                        "CreateTime": "10:00:00",
                        "CallID": f"p3-{i}",
                    }
                    for i in range(199)
                ],
            }
        return {
            "meta": {"total_count": 600},
            "rows": [
                {
                    "Phone": f"90555{page:07d}{i:03d}"[:12],
                    "ExtensionName": "x",
                    "CreateDate": "2026-07-22",
                    "CreateTime": "09:00:00",
                    "CallID": f"p{page}-{i}",
                }
                for i in range(200)
            ],
        }

    client._get_report = fake_get  # type: ignore
    result = await client.find_latest_call(
        "905466033161", date(2026, 7, 1), date(2026, 7, 25)
    )
    assert result.record is not None
    assert result.record.agent_name == "asu"
    assert result.source == "stream"
    assert max(pages) == 3  # 4. sayfaya gitme


def test_extension_not_preferred_over_dst():
    client = TonivaClient(api_key="x")
    row = {"phone": "605", "dst": "905466033161", "cnam": "asu"}
    rec = client._parse_row(client._flatten_row(row))
    assert rec is not None
    assert rec.phone == "905466033161"
