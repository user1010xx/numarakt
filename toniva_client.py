"""Toniva Public API — görüşme raporu (conversations) istemcisi."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Awaitable

import httpx

from phone_utils import digits_only, normalize_tr_phone, phones_equal

logger = logging.getLogger(__name__)

# UI / API alan adı varyasyonları — canlı şema: Phone, ExtensionName, CreateDate…
_AGENT_KEYS = (
    "ExtensionName",
    "extensionName",
    "extension_name",
    "CompletedExtensionName",
    "completedExtensionName",
    "dahiliAdi",
    "dahili_adi",
    "DAHİLİ ADI",
    "Dahili Adı",
    "agentName",
    "agent_name",
    "userName",
    "user_name",
    "agent",
    "personel",
    "personelAdi",
    "personel_adi",
    "name",
    "dahili",
    "cnam",
    "CNAM",
)
_PHONE_KEYS = (
    "Phone",
    "phone",
    "telefon",
    "TELEFON",
    "Telefon",
    "telefonNumarasi",
    "telefon_numarasi",
    "Telefon Numarası",
    "phoneNumber",
    "phone_number",
    "customerPhone",
    "customer_phone",
    "caller",
    "callerNumber",
    "caller_number",
    "callee",
    "calleeNumber",
    "callee_number",
    "msisdn",
    "externalNumber",
    "external_number",
    "connectedNumber",
    "connected_number",
    "outbound_cnum",
    "outboundCnum",
    "clid",
    "cnum",
    "did",
    "dst",
    "src",
    "number",
)
_EXTENSION_PHONE_KEYS = (
    "ExtensionNumber",
    "extensionNumber",
    "extension_number",
    "dahiliNumarasi",
    "dahili_numarasi",
    "DAHİLİ NUMARASI",
    "Dahili Numarası",
    "extension",
    "ext",
    "exten",
    "agentExtension",
    "agent_extension",
)
_DATETIME_KEYS = (
    "CreateDate",
    "createDate",
    "create_date",
    "calldate",
    "callDate",
    "call_date",
    "callDateTime",
    "call_datetime",
    "startedAt",
    "started_at",
    "startTime",
    "start_time",
    "startAt",
    "start_at",
    "createdAt",
    "created_at",
    "eventTime",
    "event_time",
    "timestamp",
    "datetime",
    "dateTime",
    "begin",
    "start",
    "ts",
)
_DATE_ONLY_KEYS = (
    "CreateDate",
    "createDate",
    "create_date",
    "tarih",
    "TARİH",
    "Tarih",
    "date",
    "day",
    "callDay",
    "call_day",
)
_TIME_ONLY_KEYS = (
    "CreateTime",
    "createTime",
    "create_time",
    "saat",
    "SAAT",
    "Saat",
    "callStartTime",
    "call_start_time",
    "startClock",
    "timeOfDay",
    "time_of_day",
    "clock",
    "hour",
    "hours",
    "callTime",
    "call_time",
    "time",
)


@dataclass(frozen=True)
class CallRecord:
    agent_name: str
    phone: str
    call_date: str
    call_time: str
    talk_seconds: int
    sort_key: datetime

    @property
    def has_conversation(self) -> bool:
        return self.talk_seconds > 0


@dataclass
class SearchResult:
    record: CallRecord | None
    row_count: int = 0
    parsed_count: int = 0
    match_count: int = 0
    sample_keys: list[str] = field(default_factory=list)
    sample_phone_values: list[str] = field(default_factory=list)
    meta_summary: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    source: str = ""  # cache | phone_filter | scan


ProgressCb = Callable[[str], Awaitable[None]] | None


class TonivaClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://crm.toniva.net/api/public/v1",
        timeout: float = 45.0,
        cache: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout, connect=20.0),
        )
        self._logged_schema = False
        self.cache = cache
        self._pagination_mode: str = "page"  # page | offset

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Public search
    # ------------------------------------------------------------------

    async def find_latest_call(
        self,
        phone: str,
        start: date,
        end: date,
        on_progress: ProgressCb = None,
        timeout_sec: float = 120.0,
    ) -> SearchResult:
        """
        1) API'ye numara ile sor (phone/Phone query) — 1–2 istek
        2) Tüm aralığı sayfala (yeniden eskiye); ilk eşleşmede DUR
        """
        target = normalize_tr_phone(phone) or phone

        # 1) Direkt numara filtresi (OpenAPI'de yok ama çoğu CRM destekler)
        if on_progress:
            await _safe_progress(on_progress, "Numara ile API sorgusu…")
        filtered = await self._try_direct_phone_query(target, start, end)
        if filtered is not None:
            return filtered

        # 2) Akış tarama — bulunca çık (yanlış "süre doldu=yok" için süre uzun)
        if on_progress:
            await _safe_progress(on_progress, "Kayıtlar taranıyor…")
        deadline = asyncio.get_event_loop().time() + max(30.0, timeout_sec)
        return await self._stream_search(
            target, start, end, on_progress, deadline=deadline
        )

    async def _try_direct_phone_query(
        self,
        target: str,
        start: date,
        end: date,
    ) -> SearchResult | None:
        """
        GET .../conversations?phone=90... 
        Filtre yok sayılıyorsa (devasa total / karışık numaralar) None.
        """
        # Tek seferlik keşif; çalışan param hafızada
        if getattr(self, "_phone_param", None) is False:
            return None

        keys: list[str]
        if isinstance(getattr(self, "_phone_param", None), str):
            keys = [self._phone_param]  # type: ignore[list-item]
        else:
            keys = ["phone", "Phone", "search"]

        variants = [target]
        if target.startswith("90") and len(target) >= 12:
            variants.append("0" + target[2:])
            variants.append(target[-10:])

        for key in keys:
            for val in variants:
                try:
                    data = await self._get_report(
                        {
                            "startDate": start.isoformat(),
                            "endDate": end.isoformat(),
                            "pageSize": 100,
                            "page": 1,
                            key: val,
                        }
                    )
                except RuntimeError as exc:
                    if "400" in str(exc):
                        break  # bu key geçersiz
                    logger.warning("direct phone query hata: %s", exc)
                    return None

                rows = self._extract_rows(data)
                meta = self._extract_meta(data)
                total = self._meta_total_count(meta)

                matches: list[CallRecord] = []
                other = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    rec = self._match_row_to_phone(self._flatten_row(row), target)
                    if rec:
                        matches.append(rec)
                    else:
                        other += 1

                # Param yok sayılmış
                if total is not None and total > 200:
                    logger.info(
                        "phone query yok sayıldı key=%s total=%s", key, total
                    )
                    break  # aynı key'in diğer varyantına gerek yok
                if len(rows) >= 20 and other > max(2, len(matches)):
                    break

                if matches:
                    self._phone_param = key
                    latest = max(matches, key=lambda r: r.sort_key)
                    logger.info(
                        "Direkt phone query OK key=%s val=%s agent=%s",
                        key,
                        val,
                        latest.agent_name,
                    )
                    return SearchResult(
                        record=latest,
                        row_count=len(rows),
                        match_count=len(matches),
                        meta_summary={
                            **meta,
                            "filter": key,
                            "filter_value": val,
                            "total_count": total,
                        },
                        source="phone_query",
                        note=f"API ?{key}=",
                    )

                # Filtre çalıştı, bu numaraya ait yok
                if total == 0 or (total is not None and total <= 5 and not matches):
                    self._phone_param = key
                    return SearchResult(
                        record=None,
                        row_count=len(rows),
                        meta_summary={**meta, "filter": key},
                        source="phone_query",
                        note=f"API filtre ({key}) ile kayıt yok",
                    )

        self._phone_param = False
        return None

    async def _stream_search(
        self,
        target: str,
        start: date,
        end: date,
        on_progress: ProgressCb,
        deadline: float | None = None,
    ) -> SearchResult:
        """
        startDate–endDate tek pencerede sayfala.
        API genelde yeniden eskiye: ilk eşleşme = en son arama → DUR.
        """
        page = 1
        # OpenAPI max 5000; API 200 dönerse effective_size ile devam
        req_size = 5000
        row_count = 0
        total: int | None = None
        seen_ids: set[str] = set()
        mode = self._pagination_mode
        sample_keys: list[str] = []
        sample_phones: list[str] = []
        last_meta: dict[str, Any] = {}
        loop = asyncio.get_event_loop()
        effective = 0

        while page <= 300:
            if deadline is not None and loop.time() >= deadline:
                return SearchResult(
                    record=None,
                    row_count=row_count,
                    source="timeout",
                    note=(
                        f"Tarama süresi doldu ({row_count} satır bakıldı). "
                        "Kayıt daha eski sayfalarda olabilir — tekrar dene."
                    ),
                    meta_summary={
                        "fetched_count": row_count,
                        "total_count": total,
                        "pages": page,
                    },
                    sample_keys=sample_keys,
                    sample_phone_values=sample_phones,
                )

            if on_progress and (page == 1 or page % 3 == 0):
                tot = f"/{total}" if total else ""
                await _safe_progress(
                    on_progress, f"Sayfa {page} (satır {row_count}{tot})…"
                )

            data = await self._get_report(
                self._page_params(start, end, page, req_size, mode)
            )
            batch = self._extract_rows(data)
            meta = self._extract_meta(data)
            last_meta = meta
            t = self._meta_total_count(meta)
            if total is None and t is not None:
                total = t

            if not batch:
                break

            if effective == 0:
                effective = len(batch)

            batch_ids = [self._row_id(r) for r in batch if isinstance(r, dict)]
            new_ids = [i for i in batch_ids if i not in seen_ids]
            if page > 1 and batch_ids and not new_ids:
                logger.warning("duplicate page mode=%s page=%s", mode, page)
                if mode == "page":
                    mode = "offset"
                    self._pagination_mode = "offset"
                    page = 1
                    seen_ids.clear()
                    row_count = 0
                    continue
                last_meta["pagination_broken"] = True
                break
            for i in batch_ids:
                seen_ids.add(i)

            row_count += len(batch)

            if not sample_keys and isinstance(batch[0], dict):
                flat0 = self._flatten_row(batch[0])
                sample_keys = list(flat0.keys())[:30]
                sample_phones = self._collect_phone_like_values(flat0, limit=6)

            page_matches: list[CallRecord] = []
            for row in batch:
                if not isinstance(row, dict):
                    continue
                rec = self._match_row_to_phone(self._flatten_row(row), target)
                if rec:
                    page_matches.append(rec)

            if page_matches:
                # Bu sayfadaki en yeni = genel en son (yeniden→eskiye sıra)
                latest = max(page_matches, key=lambda r: r.sort_key)
                logger.info(
                    "Eşleşme sayfa=%s agent=%s %s %s rows=%s",
                    page,
                    latest.agent_name,
                    latest.call_date,
                    latest.call_time,
                    row_count,
                )
                return SearchResult(
                    record=latest,
                    row_count=row_count,
                    match_count=len(page_matches),
                    sample_keys=sample_keys,
                    sample_phone_values=sample_phones,
                    meta_summary={
                        **last_meta,
                        "total_count": total,
                        "fetched_count": row_count,
                        "pages_scanned": page,
                        "early_exit": True,
                    },
                    source="stream",
                    note=f"sayfa {page}",
                )

            if total is not None and row_count >= total:
                break
            if total is not None and row_count < total:
                page += 1
                continue
            if len(batch) < (effective or req_size):
                break
            page += 1

        return SearchResult(
            record=None,
            row_count=row_count,
            sample_keys=sample_keys,
            sample_phone_values=sample_phones,
            meta_summary={
                **last_meta,
                "total_count": total,
                "fetched_count": row_count,
                "pages_scanned": page,
            },
            source="stream",
            note="Tüm sayfalar tarandı, numara yok",
        )

    def _page_params(
        self,
        start: date,
        end: date,
        page: int,
        page_size: int,
        mode: str,
    ) -> dict[str, Any]:
        base = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "pageSize": page_size,
        }
        if mode == "offset":
            base["offset"] = (page - 1) * page_size
            base["page"] = page  # bazı API ikisini birden ister
        else:
            base["page"] = page
        return base

    @staticmethod
    def _row_id(row: dict[str, Any]) -> str:
        for k in ("CallID", "callId", "call_id", "id", "uniqueid"):
            if row.get(k) not in (None, ""):
                return f"{k}:{row.get(k)}"
        # imza
        phone = row.get("Phone") or row.get("phone") or ""
        dt = row.get("CreateDate") or row.get("calldate") or ""
        tm = row.get("CreateTime") or ""
        return f"{phone}|{dt}|{tm}|{row.get('ExtensionName', '')}"

    # ------------------------------------------------------------------
    # Background sync (cache doldur)
    # ------------------------------------------------------------------

    async def sync_to_cache(
        self,
        start: date,
        end: date,
        on_progress: ProgressCb = None,
        *,
        max_days: int | None = None,
    ) -> dict[str, Any]:
        """
        Tarih aralığını cache'e yazar (arka plan).

        max_days: sadece en son N gün (hızlı ısınma). Komut gelince
        (_command_priority>0) kısa bekler, API'yi boğmaz.
        """
        if self.cache is None:
            return {"ok": False, "error": "no cache"}

        days: list[date] = []
        d = end
        while d >= start:
            days.append(d)
            d -= timedelta(days=1)
        if max_days is not None:
            days = days[: max(1, max_days)]

        total_upserted = 0
        for i, day in enumerate(days):
            if on_progress:
                await _safe_progress(
                    on_progress,
                    f"Önbellek senkron: {day.isoformat()} ({i + 1}/{len(days)})",
                )
            try:
                n = await self._ingest_day(day)
                total_upserted += n
            except Exception:
                logger.exception("sync gün hatası %s", day)
            await asyncio.sleep(0.2)

        self.cache.set_meta(
            "last_sync_at",
            datetime.now().isoformat(timespec="seconds"),
        )
        self.cache.set_meta("sync_start", start.isoformat())
        self.cache.set_meta("sync_end", end.isoformat())
        stats = self.cache.stats()
        logger.info(
            "Cache sync bitti: upsert~%s rows_in_db=%s range=%s→%s",
            total_upserted,
            stats.row_count,
            days[-1] if days else start,
            days[0] if days else end,
        )
        return {
            "ok": True,
            "upserted": total_upserted,
            "db_rows": stats.row_count,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": len(days),
        }

    async def _ingest_day(self, day: date) -> int:
        page = 1
        page_size = 200
        mode = self._pagination_mode
        seen: set[str] = set()
        upserted = 0
        total: int | None = None

        while page <= 100:
            data = await self._get_report(
                self._page_params(day, day, page, page_size, mode)
            )
            batch = self._extract_rows(data)
            meta = self._extract_meta(data)
            t = self._meta_total_count(meta)
            if total is None and t is not None:
                total = t
            if not batch:
                break

            ids = [self._row_id(r) for r in batch if isinstance(r, dict)]
            new_ids = [i for i in ids if i not in seen]
            if page > 1 and ids and not new_ids:
                if mode == "page":
                    mode = "offset"
                    self._pagination_mode = "offset"
                    page = 1
                    seen.clear()
                    continue
                break
            for i in ids:
                seen.add(i)

            if self.cache is not None:
                upserted += self.cache.upsert_raw_rows(
                    [r for r in batch if isinstance(r, dict)],
                    parse_fn=lambda r: self._parse_row(self._flatten_row(r)),
                )

            if total is not None and len(seen) >= total:
                break
            if len(batch) < page_size:
                break
            page += 1

        return upserted

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _get_report(self, params: dict[str, Any]) -> Any:
        url = "/reports/conversations"
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Toniva API bağlantı hatası: {exc}") from exc

            if resp.status_code == 429:
                raw_retry = resp.headers.get("Retry-After", "2")
                try:
                    wait_s = max(1, min(5, int(float(raw_retry))))
                except ValueError:
                    wait_s = 2
                logger.warning("429 rate limit, %ss (deneme %s)", wait_s, attempt + 1)
                await asyncio.sleep(wait_s)
                last_err = RuntimeError(
                    f"Toniva rate limit. Retry-After: {raw_retry}"
                )
                continue

            if resp.status_code >= 400:
                detail = resp.text[:300]
                raise RuntimeError(f"Toniva API hata {resp.status_code}: {detail}")

            try:
                return resp.json()
            except ValueError as exc:
                raise RuntimeError("Toniva API geçersiz JSON") from exc

        raise last_err or RuntimeError("Toniva rate limit")

    # ------------------------------------------------------------------
    # Row parse (şema)
    # ------------------------------------------------------------------

    def _match_row_to_phone(
        self, flat: dict[str, Any], target: str
    ) -> CallRecord | None:
        row_hit = self._row_contains_phone(flat, target)
        rec = self._parse_row(flat)
        if rec is None:
            if not row_hit:
                return None
            rec = self._record_from_flat(flat, phone_fallback=target)
            if rec is None:
                return None
        elif not (row_hit or phones_equal(rec.phone, target)):
            return None
        if not phones_equal(rec.phone, target):
            rec = CallRecord(
                agent_name=rec.agent_name,
                phone=target,
                call_date=rec.call_date,
                call_time=rec.call_time,
                talk_seconds=rec.talk_seconds,
                sort_key=rec.sort_key,
            )
        return rec

    def _parse_row(self, row: dict[str, Any]) -> CallRecord | None:
        phone_raw = self._extract_external_phone(row)
        if phone_raw is None:
            for nest in ("party", "remote", "customer", "contact"):
                nested = row.get(nest)
                if isinstance(nested, dict):
                    phone_raw = self._extract_external_phone(nested)
                    if phone_raw is not None:
                        break
        if phone_raw is None:
            return None
        phone_str = str(phone_raw).strip()
        if not phone_str or phone_str in ("-", "—", "–"):
            return None
        return self._record_from_flat(row, phone_fallback=phone_str)

    def _record_from_flat(
        self, row: dict[str, Any], *, phone_fallback: str
    ) -> CallRecord | None:
        phone_str = str(phone_fallback).strip()
        if not phone_str or phone_str in ("-", "—", "–"):
            return None
        agent = self._pick(row, _AGENT_KEYS)
        if agent is None:
            agent = self._pick(row, ("cnam", "CNAM", "agentname", "memberName"))
        agent_str = str(agent).strip() if agent is not None else "—"
        if not agent_str:
            agent_str = "—"
        talk_seconds = self._extract_talk_seconds(row)
        sort_dt, date_disp, time_disp = self._extract_datetime(row)
        display_phone = normalize_tr_phone(phone_str) or digits_only(phone_str) or phone_str
        return CallRecord(
            agent_name=agent_str,
            phone=display_phone,
            call_date=date_disp,
            call_time=time_disp,
            talk_seconds=talk_seconds,
            sort_key=sort_dt,
        )

    @classmethod
    def _extract_external_phone(cls, row: dict[str, Any]) -> Any | None:
        candidates: list[tuple[int, int, Any]] = []

        def add(val: Any, *, key_name: str, key_priority: int) -> None:
            if val is None or val == "":
                return
            s = str(val).strip()
            if not s or s in ("-", "—", "–"):
                return
            score = cls._phone_value_score(s, key_name=key_name)
            if score < 0:
                return
            candidates.append((score, -key_priority, val))

        for i, k in enumerate(_PHONE_KEYS):
            raw = cls._pick(row, (k,))
            if raw is not None:
                add(raw, key_name=k, key_priority=i)

        for key, val in row.items():
            if val is None or val == "" or isinstance(val, (dict, list)):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_extension_key(fk):
                add(val, key_name=str(key), key_priority=500)
                continue
            if cls._key_looks_like_phone_field(fk):
                add(val, key_name=str(key), key_priority=100)

        for i, k in enumerate(_EXTENSION_PHONE_KEYS):
            raw = cls._pick(row, (k,))
            if raw is not None:
                add(raw, key_name=k, key_priority=1000 + i)

        if not any(s >= 50 for s, _, _ in candidates):
            for key, val in row.items():
                if val is None or isinstance(val, (dict, list, bool)):
                    continue
                s = str(val).strip()
                if s and normalize_tr_phone(s):
                    add(s, key_name=str(key), key_priority=200)

        if not candidates:
            return None
        return max(candidates, key=lambda t: (t[0], t[1]))[2]

    @staticmethod
    def _is_extension_key(folded_key: str) -> bool:
        fk = folded_key
        if "external" in fk:
            return False
        if any(x in fk for x in ("dahili", "extension", "exten")):
            return True
        return fk in ("ext", "src") or fk.endswith("ext")

    @staticmethod
    def _key_looks_like_phone_field(folded_key: str) -> bool:
        fk = folded_key
        if any(
            x in fk
            for x in (
                "telefon",
                "phone",
                "msisdn",
                "caller",
                "callee",
                "clid",
                "cnum",
                "dst",
                "did",
            )
        ):
            return True
        if fk in ("number", "src", "dst"):
            return True
        return "numara" in fk and "dahili" not in fk

    @classmethod
    def _phone_value_score(cls, value: str, *, key_name: str) -> int:
        d = re.sub(r"\D+", "", value)
        if not d:
            return -1
        fk = cls._fold_key(key_name)
        ext_key = cls._is_extension_key(fk)
        norm = normalize_tr_phone(value)
        if norm and len(norm) >= 12:
            base = 120 if norm[2:3] == "5" else 100
            return 20 if ext_key else base
        if len(d) >= 10:
            if ext_key:
                return 25
            return 90 if d[-10:].startswith("5") else 70
        if len(d) >= 7:
            return 10 if ext_key else 30
        if 3 <= len(d) <= 6:
            return 5 if ext_key else 8
        return -1

    @classmethod
    def _row_contains_phone(cls, row: dict[str, Any], target: str) -> bool:
        for val in row.values():
            if val is None or isinstance(val, (dict, list, bool)):
                continue
            if phones_equal(str(val), target):
                return True
            d = digits_only(str(val))
            if len(d) >= 10 and phones_equal(d[-10:], target):
                return True
            for m in re.finditer(r"\d{10,14}", d if d else str(val)):
                if phones_equal(m.group(0), target):
                    return True
        return False

    @classmethod
    def _collect_phone_like_values(
        cls, row: dict[str, Any], *, limit: int = 8
    ) -> list[str]:
        out: list[str] = []
        for key, val in row.items():
            if val is None or isinstance(val, (dict, list, bool)):
                continue
            s = str(val).strip()
            if not s or s in ("-", "—", "–"):
                continue
            d = digits_only(s)
            fk = cls._fold_key(str(key))
            if len(d) >= 7 or cls._key_looks_like_phone_field(fk) or cls._is_extension_key(fk):
                out.append(f"{key}={s[:32]}")
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # extract rows / meta / datetime / duration
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_rows(data: Any) -> list[dict[str, Any]]:
        if data is None:
            return []
        if isinstance(data, list):
            return TonivaClient._normalize_row_list(data, columns=None)
        if not isinstance(data, dict):
            return []

        meta = data.get("meta") or data.get("metadata") or {}
        columns = None
        if isinstance(meta, dict):
            columns = meta.get("columns") or meta.get("fields") or meta.get("headers")

        for key in (
            "rows",
            "data",
            "items",
            "results",
            "records",
            "conversations",
            "content",
            "list",
            "values",
            "payload",
        ):
            val = data.get(key)
            if isinstance(val, list):
                return TonivaClient._normalize_row_list(val, columns)
            if isinstance(val, dict):
                for inner in ("rows", "data", "items", "results", "records", "content"):
                    if isinstance(val.get(inner), list):
                        return TonivaClient._normalize_row_list(val[inner], columns)

        report = data.get("report")
        if isinstance(report, dict):
            return TonivaClient._extract_rows(report)

        best = TonivaClient._deep_find_row_list(data)
        if best:
            return TonivaClient._normalize_row_list(best, columns)
        return []

    @staticmethod
    def _deep_find_row_list(data: Any, depth: int = 0) -> list[Any] | None:
        if depth > 4 or data is None:
            return None
        if isinstance(data, list) and data:
            if all(isinstance(x, dict) for x in data[:5]):
                return data
            if all(isinstance(x, (list, tuple)) for x in data[:5]):
                return data
            return None
        if not isinstance(data, dict):
            return None
        best: list[Any] | None = None
        best_n = 0
        for v in data.values():
            found = TonivaClient._deep_find_row_list(v, depth + 1)
            if found is not None and len(found) > best_n:
                best = found
                best_n = len(found)
        return best

    @staticmethod
    def _normalize_row_list(rows: list[Any], columns: Any) -> list[dict[str, Any]]:
        col_names: list[str] | None = None
        if isinstance(columns, list) and columns:
            col_names = [TonivaClient._column_to_name(c) for c in columns]
        elif isinstance(columns, dict) and isinstance(columns.get("fields"), list):
            col_names = [TonivaClient._column_to_name(c) for c in columns["fields"]]

        out: list[dict[str, Any]] = []
        for r in rows:
            if isinstance(r, dict):
                out.append(r)
            elif isinstance(r, (list, tuple)) and col_names:
                n = min(len(col_names), len(r))
                out.append({col_names[i]: r[i] for i in range(n)})
        return out

    @staticmethod
    def _column_to_name(col: Any) -> str:
        if isinstance(col, dict):
            for k in ("key", "name", "field", "label", "id", "slug"):
                v = col.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
            return str(col)
        return str(col)

    @classmethod
    def _flatten_row(cls, row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        flat: dict[str, Any] = {}
        for k, v in row.items():
            key = f"{prefix}{k}" if not prefix else f"{prefix}_{k}"
            if isinstance(v, dict):
                flat[key] = v
                flat.update(cls._flatten_row(v, prefix=str(k)))
                for nk, nv in v.items():
                    if not isinstance(nv, (dict, list)):
                        flat.setdefault(str(nk), nv)
            else:
                flat[key if prefix else str(k)] = v
        return flat

    @staticmethod
    def _extract_meta(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        meta = data.get("meta") or data.get("metadata") or {}
        return meta if isinstance(meta, dict) else {}

    @staticmethod
    def _meta_total_count(meta: dict[str, Any]) -> int | None:
        for k in ("total_count", "totalCount", "total", "TotalCount", "count"):
            v = meta.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and int(v) >= 0:
                return int(v)
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return None

    @staticmethod
    def _fold_key(key: str) -> str:
        s = str(key).strip().lower().replace(" ", "").replace("_", "")
        s = s.replace("i̇", "i").replace("ı", "i")
        for src, dst in (("ğ", "g"), ("ü", "u"), ("ş", "s"), ("ö", "o"), ("ç", "c")):
            s = s.replace(src, dst)
        return s

    @classmethod
    def _pick(cls, row: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for k in keys:
            if k in row and row[k] not in (None, ""):
                return row[k]
        folded_map = {
            cls._fold_key(k): v for k, v in row.items() if v not in (None, "")
        }
        for k in keys:
            v = folded_map.get(cls._fold_key(k))
            if v not in (None, ""):
                return v
        return None

    def _extract_datetime(self, row: dict[str, Any]) -> tuple[datetime, str, str]:
        clock_raw = self._find_clock_value(row)
        best: tuple[datetime, str, str] | None = None

        for key in _DATETIME_KEYS:
            raw = self._pick(row, (key,))
            if raw is None:
                continue
            parsed = self._try_parse_single_datetime(raw)
            if parsed is None:
                continue
            merged = self._apply_clock_if_needed(parsed, clock_raw)
            best = self._prefer_datetime(best, merged)

        date_raw = self._pick(row, _DATE_ONLY_KEYS)
        if date_raw is not None:
            resolved = self._resolve_datetime(date_raw, clock_raw)
            if resolved[0] != datetime.min:
                best = self._prefer_datetime(best, resolved)

        if best is not None:
            return best
        return self._resolve_datetime(date_raw, clock_raw)

    @classmethod
    def _find_clock_value(cls, row: dict[str, Any]) -> Any:
        for k in _TIME_ONLY_KEYS:
            if cls._is_duration_like_key(cls._fold_key(k)):
                continue
            raw = cls._pick(row, (k,))
            if raw is None:
                continue
            if cls._is_plausible_clock(raw):
                return raw
        return None

    @staticmethod
    def _is_duration_like_key(folded_key: str) -> bool:
        fk = folded_key
        if fk in ("calltime", "ringtime", "waittime"):
            return True
        return any(
            x in fk
            for x in (
                "duration",
                "sure",
                "suresi",
                "ring",
                "caldirma",
                "billsec",
                "talk",
                "gorusme",
                "wait",
                "hold",
            )
        )

    @classmethod
    def _apply_clock_if_needed(
        cls,
        parsed: tuple[datetime, str, str],
        clock_raw: Any,
    ) -> tuple[datetime, str, str]:
        if clock_raw is None:
            return parsed
        dt, _d, _t = parsed
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            t_part = _parse_time_only(cls._clock_to_str(clock_raw))
            if t_part is not None and not (
                t_part.hour == 0 and t_part.minute == 0 and t_part.second == 0
            ):
                dt2 = datetime.combine(dt.date(), t_part)
                return dt2, dt2.strftime("%d.%m.%Y"), dt2.strftime("%H:%M:%S")
        return parsed

    @staticmethod
    def _prefer_datetime(
        current: tuple[datetime, str, str] | None,
        candidate: tuple[datetime, str, str],
    ) -> tuple[datetime, str, str]:
        if current is None:
            return candidate
        c_dt, _, _ = current
        n_dt, _, _ = candidate
        c_mid = c_dt.hour == 0 and c_dt.minute == 0 and c_dt.second == 0
        n_mid = n_dt.hour == 0 and n_dt.minute == 0 and n_dt.second == 0
        if c_mid and not n_mid:
            return candidate
        if not c_mid and n_mid:
            return current
        return candidate if n_dt >= c_dt else current

    @classmethod
    def _clock_to_str(cls, raw: Any) -> str:
        if isinstance(raw, str):
            return raw.strip().replace(".", ":")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            n = int(raw)
            if 0 < n <= 235959:
                s = f"{n:06d}"
                return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"
        return str(raw).strip()

    @classmethod
    def _is_plausible_clock(cls, raw: Any) -> bool:
        if raw is None or raw == "" or isinstance(raw, bool):
            return False
        if isinstance(raw, (int, float)):
            n = int(raw)
            if n <= 0 or n > 235959:
                return False
            s = f"{n:06d}"
            hh, mm, ss = int(s[0:2]), int(s[2:4]), int(s[4:6])
            return hh <= 23 and mm <= 59 and ss <= 59
        s = str(raw).strip().replace(".", ":")
        if not s or s in ("0", "0.0", "-", "—", "–"):
            return False
        return bool(re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s))

    @classmethod
    def _try_parse_single_datetime(cls, raw: Any) -> tuple[datetime, str, str] | None:
        if raw is None or raw == "" or raw in ("-", "—", "–") or isinstance(raw, bool):
            return None
        if isinstance(raw, datetime):
            dt = raw.replace(tzinfo=None) if raw.tzinfo else raw
            return dt, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")
        if isinstance(raw, date) and not isinstance(raw, datetime):
            dt = datetime.combine(raw, time(0, 0, 0))
            return dt, dt.strftime("%d.%m.%Y"), "00:00:00"
        if isinstance(raw, (int, float)):
            ts = float(raw)
            if ts < 1_000_000_000:
                return None
            if ts > 1e12:
                ts /= 1000.0
            try:
                dt = datetime.fromtimestamp(ts)
                return dt, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")
            except (OSError, OverflowError, ValueError):
                return None
        s = str(raw).strip()
        if not s or s in ("0", "None"):
            return None
        if re.fullmatch(r"\d{1,2}[:.]\d{2}([:.]\d{2})?", s):
            return None
        resolved = cls._resolve_datetime(s, None)
        if resolved[0] != datetime.min:
            return resolved
        return None

    @staticmethod
    def _resolve_datetime(
        date_raw: Any,
        time_raw: Any,
    ) -> tuple[datetime, str, str]:
        fallback = datetime.min
        if isinstance(date_raw, (int, float)):
            ts = float(date_raw)
            if ts > 1e12:
                ts /= 1000.0
            try:
                dt = datetime.fromtimestamp(ts)
                return dt, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")
            except (OSError, OverflowError, ValueError):
                pass

        date_str = str(date_raw).strip() if date_raw is not None else ""
        time_str = str(time_raw).strip() if time_raw is not None else ""
        iso_try = date_str.replace("Z", "+00:00")
        for candidate in (iso_try, f"{date_str} {time_str}".strip()):
            if not candidate or candidate == "None":
                continue
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M",
                "%d.%m.%Y %H:%M:%S",
                "%d.%m.%Y %H:%M",
                "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y %H:%M",
            ):
                try:
                    dt = datetime.strptime(candidate, fmt)
                    return dt, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")
                except ValueError:
                    continue
            try:
                dt = datetime.fromisoformat(candidate)
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
                return dt, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")
            except ValueError:
                pass

        d_part = _parse_date_only(date_str)
        t_part = _parse_time_only(time_str) if time_str else None
        if d_part is not None:
            t_use = t_part or time(0, 0, 0)
            dt = datetime.combine(d_part, t_use)
            return (
                dt,
                d_part.strftime("%d.%m.%Y"),
                t_use.strftime("%H:%M:%S") if t_part else (time_str or "00:00:00"),
            )

        parsed_ui = _parse_turkish_long_date(date_str)
        if parsed_ui is not None:
            t_use = _parse_time_only(time_str) or time(0, 0, 0)
            dt = datetime.combine(parsed_ui, t_use)
            return (
                dt,
                parsed_ui.strftime("%d.%m.%Y"),
                t_use.strftime("%H:%M:%S")
                if _parse_time_only(time_str)
                else (time_str or "00:00:00"),
            )
        return fallback, date_str or "—", time_str or "—"

    def _extract_talk_seconds(self, row: dict[str, Any]) -> int:
        talk = self._seconds_from_named_fields(row, kind="talk")
        if talk > 0:
            return talk
        ring = self._seconds_from_named_fields(row, kind="ring")
        total = self._seconds_from_named_fields(row, kind="total")
        if total > 0 and ring > 0 and total > ring:
            derived = total - ring
            if not (derived > 300 and derived > max(ring, 1) * 20):
                return derived
        if total > 0 and ring == 0 and total <= 3600:
            return total
        return 0

    @classmethod
    def _seconds_from_named_fields(cls, row: dict[str, Any], *, kind: str) -> int:
        best = 0
        for key, val in row.items():
            if val in (None, "", "-", "—", "–"):
                continue
            fk = cls._fold_key(str(key))
            if not cls._key_matches_duration_kind(fk, kind):
                continue
            if fk in ("time", "saat", "date", "tarih") or fk.endswith("clock"):
                continue
            sec = cls._parse_duration_seconds(val)
            if isinstance(val, str) and re.fullmatch(
                r"\d{1,2}:\d{2}:\d{2}",
                val.strip().replace(".", ":").replace("：", ":"),
            ):
                parts = [
                    int(p)
                    for p in val.strip().replace(".", ":").replace("：", ":").split(":")
                ]
                if parts[0] >= 3:
                    continue
            if sec <= 0 or sec > 6 * 3600:
                continue
            best = max(best, sec)
        return best

    @classmethod
    def _key_matches_duration_kind(cls, folded_key: str, kind: str) -> bool:
        fk = folded_key
        if "record" in fk and kind == "ring":
            return False
        if kind == "ring":
            return any(x in fk for x in ("ring", "caldir", "ringing")) and "record" not in fk
        if kind == "talk":
            if fk in ("calltime", "call_time"):
                return True
            if any(
                x in fk
                for x in (
                    "gorusme",
                    "talkduration",
                    "talksec",
                    "talktime",
                    "billsec",
                    "conversationduration",
                    "connectedduration",
                    "bridgeduration",
                    "speakingduration",
                )
            ):
                return True
            if "talk" in fk and any(x in fk for x in ("duration", "sec", "sure", "time")):
                return not cls._key_matches_duration_kind(fk, "ring")
            return False
        if kind == "total":
            if any(x in fk for x in ("queue", "hold", "wait", "pause")):
                return False
            if fk in ("duration", "callduration", "totalduration", "totalsec", "length"):
                return True
            if fk.endswith("duration") and not any(
                x in fk
                for x in (
                    "talk",
                    "ring",
                    "gorusme",
                    "caldir",
                    "bill",
                    "conversation",
                    "connected",
                    "bridge",
                    "speaking",
                    "record",
                )
            ):
                return True
        return False

    @staticmethod
    def _parse_duration_seconds(raw: Any) -> int:
        if raw is None or raw == "" or raw in ("-", "—", "–") or isinstance(raw, bool):
            return 0
        if isinstance(raw, (int, float)):
            n = float(raw)
            if n <= 0:
                return 0
            if n > 6 * 3600:
                return max(0, int(round(n / 1000.0)))
            return int(n)
        s = str(raw).strip().replace("：", ":")
        if not s:
            return 0
        s_norm = (
            s.replace(".", ":")
            if re.fullmatch(r"\d{1,2}[.:]\d{2}([.:]\d{2})?", s)
            else s
        )
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s_norm):
            parts = [int(p) for p in s_norm.split(":")]
            if len(parts) == 3:
                h, m, sec = parts
                return max(0, h * 3600 + m * 60 + sec)
            if len(parts) == 2:
                m, sec = parts
                return max(0, m * 60 + sec)
        if re.fullmatch(r"\d+", s):
            return TonivaClient._parse_duration_seconds(int(s))
        return 0

    # test helpers
    @staticmethod
    def _date_chunks(start: date, end: date, max_days: int = 14) -> list[tuple[date, date]]:
        if end < start:
            return []
        out: list[tuple[date, date]] = []
        cur = start
        while cur <= end:
            chunk_end = min(cur + timedelta(days=max_days - 1), end)
            out.append((cur, chunk_end))
            cur = chunk_end + timedelta(days=1)
        return out


async def _safe_progress(cb: ProgressCb, msg: str) -> None:
    if cb is None:
        return
    try:
        await cb(msg)
    except Exception:
        logger.debug("progress callback hata", exc_info=True)


def _parse_date_only(s: str) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return _parse_turkish_long_date(s)


def _parse_time_only(s: str) -> time | None:
    if not s:
        return None
    s = s.strip().replace(".", ":")
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _parse_turkish_long_date(s: str) -> date | None:
    if not s:
        return None
    m = re.search(
        r"(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s+(\d{4})",
        s,
        re.UNICODE,
    )
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower().replace("i̇", "i")
    month_name = (
        month_name.replace("ı", "i")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ş", "s")
        .replace("ö", "o")
        .replace("ç", "c")
    )
    folded = {
        "ocak": 1,
        "subat": 2,
        "mart": 3,
        "nisan": 4,
        "mayis": 5,
        "haziran": 6,
        "temmuz": 7,
        "agustos": 8,
        "eylul": 9,
        "ekim": 10,
        "kasim": 11,
        "aralik": 12,
    }
    month = folded.get(month_name)
    if not month:
        return None
    year = int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None
