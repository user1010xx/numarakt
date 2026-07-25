"""Yerel SQLite önbellek — numara ile O(1) arama."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phone_utils import normalize_tr_phone
from toniva_client import CallRecord

logger = logging.getLogger(__name__)


@dataclass
class CacheStats:
    row_count: int
    min_date: str | None
    max_date: str | None
    last_sync_at: str | None


class CallCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS calls (
                        call_id TEXT PRIMARY KEY,
                        phone_norm TEXT NOT NULL,
                        phone_last10 TEXT NOT NULL,
                        agent_name TEXT NOT NULL,
                        call_date TEXT NOT NULL,
                        call_time TEXT NOT NULL,
                        talk_seconds INTEGER NOT NULL DEFAULT 0,
                        sort_key TEXT NOT NULL,
                        synced_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_calls_phone
                        ON calls(phone_norm);
                    CREATE INDEX IF NOT EXISTS idx_calls_last10
                        ON calls(phone_last10);
                    CREATE INDEX IF NOT EXISTS idx_calls_sort
                        ON calls(sort_key DESC);

                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
                conn.commit()
            finally:
                conn.close()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)
                ).fetchone()
                return str(row["value"]) if row else None
            finally:
                conn.close()

    def stats(self) -> CacheStats:
        with self._lock:
            conn = self._connect()
            try:
                n = conn.execute("SELECT COUNT(*) AS c FROM calls").fetchone()["c"]
                bounds = conn.execute(
                    "SELECT MIN(substr(sort_key,1,10)) AS mn, "
                    "MAX(substr(sort_key,1,10)) AS mx FROM calls"
                ).fetchone()
                return CacheStats(
                    row_count=int(n),
                    min_date=bounds["mn"],
                    max_date=bounds["mx"],
                    last_sync_at=self.get_meta("last_sync_at"),
                )
            finally:
                conn.close()

    def find_latest(self, phone: str) -> CallRecord | None:
        target = normalize_tr_phone(phone) or phone
        last10 = target[-10:] if len(target) >= 10 else target
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT * FROM calls
                    WHERE phone_norm = ? OR phone_last10 = ?
                    ORDER BY sort_key DESC
                    LIMIT 1
                    """,
                    (target, last10),
                ).fetchone()
            finally:
                conn.close()

        if not row:
            return None
        try:
            sk = datetime.fromisoformat(row["sort_key"])
        except ValueError:
            sk = datetime.min
        return CallRecord(
            agent_name=row["agent_name"],
            phone=row["phone_norm"],
            call_date=row["call_date"],
            call_time=row["call_time"],
            talk_seconds=int(row["talk_seconds"] or 0),
            sort_key=sk,
        )

    def upsert_records(self, records: list[CallRecord], *, id_prefix: str = "") -> int:
        """CallRecord listesini yazar. call_id yoksa sort+phone ile üretir."""
        if not records:
            return 0
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        rows: list[tuple[Any, ...]] = []
        for i, rec in enumerate(records):
            phone = normalize_tr_phone(rec.phone) or rec.phone
            last10 = phone[-10:] if len(phone) >= 10 else phone
            cid = f"{id_prefix}{phone}|{rec.sort_key.isoformat()}|{rec.agent_name}|{i}"
            rows.append(
                (
                    cid,
                    phone,
                    last10,
                    rec.agent_name,
                    rec.call_date,
                    rec.call_time,
                    int(rec.talk_seconds),
                    rec.sort_key.isoformat(timespec="seconds"),
                    now,
                )
            )
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    """
                    INSERT INTO calls(
                        call_id, phone_norm, phone_last10, agent_name,
                        call_date, call_time, talk_seconds, sort_key, synced_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(call_id) DO UPDATE SET
                        agent_name=excluded.agent_name,
                        call_date=excluded.call_date,
                        call_time=excluded.call_time,
                        talk_seconds=excluded.talk_seconds,
                        sort_key=excluded.sort_key,
                        synced_at=excluded.synced_at
                    """,
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return len(rows)

    def upsert_raw_rows(
        self,
        raw_rows: list[dict[str, Any]],
        parse_fn,
    ) -> int:
        """Ham API satırlarını parse edip yazar (CallID varsa kararlı anahtar)."""
        if not raw_rows:
            return 0
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        batch: list[tuple[Any, ...]] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            rec = parse_fn(row)
            if rec is None:
                continue
            phone = normalize_tr_phone(rec.phone) or rec.phone
            if not phone or len(phone) < 10:
                continue
            last10 = phone[-10:]
            cid = None
            for k in ("CallID", "callId", "call_id", "id", "uniqueid"):
                if row.get(k) not in (None, ""):
                    cid = f"{k}:{row.get(k)}"
                    break
            if not cid:
                cid = f"{phone}|{rec.sort_key.isoformat()}|{rec.agent_name}"
            batch.append(
                (
                    cid,
                    phone,
                    last10,
                    rec.agent_name,
                    rec.call_date,
                    rec.call_time,
                    int(rec.talk_seconds),
                    rec.sort_key.isoformat(timespec="seconds"),
                    now,
                )
            )
        if not batch:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    """
                    INSERT INTO calls(
                        call_id, phone_norm, phone_last10, agent_name,
                        call_date, call_time, talk_seconds, sort_key, synced_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(call_id) DO UPDATE SET
                        agent_name=excluded.agent_name,
                        phone_norm=excluded.phone_norm,
                        phone_last10=excluded.phone_last10,
                        call_date=excluded.call_date,
                        call_time=excluded.call_time,
                        talk_seconds=excluded.talk_seconds,
                        sort_key=excluded.sort_key,
                        synced_at=excluded.synced_at
                    """,
                    batch,
                )
                conn.commit()
            finally:
                conn.close()
        return len(batch)
