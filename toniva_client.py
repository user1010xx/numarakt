"""Toniva Public API — görüşme raporu (conversations) istemcisi."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx

from phone_utils import digits_only, normalize_tr_phone, phones_equal

logger = logging.getLogger(__name__)

# UI / API alan adı varyasyonları
# Canlı şema (Toniva conversations): ExtensionName, Phone, CreateDate, CreateTime, …
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
# Dış numara adayları (öncelik: müşteri / harici hat — dahili DEĞİL)
_PHONE_KEYS = (
    "Phone",  # canlı Toniva conversations
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
# Dahili / extension — dış numara adayı olarak düşük öncelik
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
# Tam tarih+saat veya yalnızca tarih taşıyabilen alanlar (öncelik sırası)
_DATETIME_KEYS = (
    "CreateDate",  # canlı Toniva — bazen yalnız gün; CreateTime ile birleşir
    "createDate",
    "create_date",
    "calldate",  # FreePBX / Asterisk CDR — en sık
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
    "CreateTime",  # canlı Toniva duvar saati
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
    # CallTime canlı şemada GÖRÜŞME SÜRESİ — saat değil; sonda ve clock check ile elenir
    "callTime",
    "call_time",
    # bare "time" en sonda — API bazen time=0 (süre) gönderiyor, saat değil
    "time",
)
# Görüşme (talk) — çaldırma/ring YOK. Önce spesifik, sonda genel.
_TALK_DURATION_KEYS = (
    "CallTime",  # canlı Toniva = görüşme süresi
    "callTime",
    "call_time",
    "gorusmeSuresi",
    "görüşmeSüresi",
    "gorusme_suresi",
    "GÖRÜŞME SÜRESİ",
    "Görüşme Süresi",
    "talkDuration",
    "talk_duration",
    "talkSec",
    "talk_sec",
    "talkSeconds",
    "talk_seconds",
    "billsec",
    "billSec",
    "bill_sec",
    "billedSeconds",
    "answeredDuration",
    "answered_duration",
    "answeredSec",
    "answered_sec",
    "answerSec",
    "answer_sec",
    "conversationDuration",
    "conversation_duration",
    "talkTime",
    "talk_time",
    "connectedDuration",
    "connected_duration",
    "connectedSec",
    "connected_sec",
    "bridgeDuration",
    "bridge_duration",
    "bridgeTime",
    "bridge_time",
    "bridgeSec",
    "bridge_sec",
    "speakingDuration",
    "speaking_duration",
    "handleTime",
    "handle_time",
    "serviceTime",
    "service_time",
    "inCallDuration",
    "in_call_duration",
    "activeDuration",
    "active_duration",
)
# Toplam çağrı süresi (ring+talk olabilir) — yalnızca yedek
_TOTAL_DURATION_KEYS = (
    "callDuration",
    "call_duration",
    "totalDuration",
    "total_duration",
    "duration",
    "totalSec",
    "total_sec",
    "length",
    "len",
)
# Çaldırma — görüşmeden düşmek için
_RING_DURATION_KEYS = (
    "RingTime",
    "ringTime",
    "ring_time",
    "WaitTime",
    "waitTime",
    "wait_time",
    "caldirmaSuresi",
    "çaldırmaSüresi",
    "caldirma_suresi",
    "ÇALDIRMA SÜRESİ",
    "ringDuration",
    "ring_duration",
    "ringSec",
    "ring_sec",
    "ringSeconds",
    "ring_seconds",
    "ringingDuration",
    "ringing_duration",
    "ringingTime",
    "ringing_time",
    "waitDuration",
    "wait_duration",
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
    """find_latest_call çıktısı — bulunamadı durumunda teşhis için sayaçlar."""

    record: CallRecord | None
    row_count: int = 0
    parsed_count: int = 0
    match_count: int = 0
    sample_keys: list[str] = field(default_factory=list)
    sample_phone_values: list[str] = field(default_factory=list)
    meta_summary: dict[str, Any] = field(default_factory=dict)
    note: str = ""


class TonivaClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://crm.toniva.net/api/public/v1",
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        self._logged_schema = False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def find_latest_call(
        self,
        phone: str,
        start: date,
        end: date,
    ) -> SearchResult:
        """Son N gün içinde numaraya ait en son kaydı bul (teşhis sayaçlarıyla)."""
        target = normalize_tr_phone(phone) or phone
        rows, meta = await self.fetch_conversations(start, end)

        sample_keys: list[str] = []
        sample_phones: list[str] = []
        if rows and isinstance(rows[0], dict):
            flat0 = self._flatten_row(rows[0])
            sample_keys = list(flat0.keys())[:40]
            sample_phones = self._collect_phone_like_values(flat0, limit=8)

        if not rows:
            # Ham JSON şeklini bir kez logla
            logger.warning(
                "conversations boş döndü (%s → %s) meta=%s",
                start.isoformat(),
                end.isoformat(),
                meta,
            )
            return SearchResult(
                record=None,
                row_count=0,
                meta_summary=meta,
                sample_keys=sample_keys,
                sample_phone_values=sample_phones,
                note="API 0 satır döndü (şema/tenant/tarih veya satır çıkarımı)",
            )

        matches: list[tuple[CallRecord, dict[str, Any]]] = []
        parsed = 0
        scanned_match = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            flat = self._flatten_row(row)

            # 1) Önce satırın HER skaler değerinde numara ara (alan adı bağımsız)
            row_hit = self._row_contains_phone(flat, target)
            if row_hit:
                scanned_match += 1

            rec = self._parse_row(flat)
            if rec is None:
                if not row_hit:
                    continue
                # Numara satırda var ama parse başarısız → yine de kayıt üret
                rec = self._record_from_flat(flat, phone_fallback=target)
                if rec is None:
                    continue

            parsed += 1
            if row_hit or phones_equal(rec.phone, target):
                # Gösterim telefonunu hedefe sabitle (dahili gölgelemesi kalmasın)
                if not phones_equal(rec.phone, target):
                    rec = CallRecord(
                        agent_name=rec.agent_name,
                        phone=target,
                        call_date=rec.call_date,
                        call_time=rec.call_time,
                        talk_seconds=rec.talk_seconds,
                        sort_key=rec.sort_key,
                    )
                matches.append((rec, flat))

        if parsed == 0 and scanned_match == 0:
            logger.error(
                "Satırlar geldi ama telefon okunamadı. keys=%s sample_phones=%s",
                sample_keys[:30],
                sample_phones,
            )
            raise RuntimeError(
                "Toniva satırları geldi ama telefon alanı okunamadı. "
                f"Örnek alan adları: {sample_keys[:30]}. "
                f"Örnek değerler: {sample_phones[:8]}. "
                "toniva_client alan eşlemesi güncellenmeli."
            )

        if not matches:
            # Daha fazla örnek telefon topla (ilk 20 satır)
            for row in rows[:20]:
                if isinstance(row, dict):
                    sample_phones.extend(
                        self._collect_phone_like_values(self._flatten_row(row), limit=3)
                    )
            # tekilleştir
            seen: set[str] = set()
            uniq_phones: list[str] = []
            for p in sample_phones:
                if p not in seen:
                    seen.add(p)
                    uniq_phones.append(p)
            sample_phones = uniq_phones[:12]

            logger.info(
                "Numara eşleşmedi: target=%s satır=%s parse=%s scan_hit=%s örnek_tel=%s",
                target,
                len(rows),
                parsed,
                scanned_match,
                sample_phones,
            )
            return SearchResult(
                record=None,
                row_count=len(rows),
                parsed_count=parsed,
                match_count=0,
                sample_keys=sample_keys,
                sample_phone_values=sample_phones,
                meta_summary=meta,
                note="Satırlar var, numara hiçbir alanda yok (veya maskeli)",
            )

        # Aynı numarayı birden fazla personel aramış olabilir → en son arama
        idx, (latest, raw) = max(
            enumerate(matches),
            key=lambda pair: (pair[1][0].sort_key, pair[0]),
        )
        logger.info(
            "En son arama seçildi: phone=%s agent=%s at=%s %s talk=%ss (eşleşme=%s)",
            latest.phone,
            latest.agent_name,
            latest.call_date,
            latest.call_time,
            latest.talk_seconds,
            len(matches),
        )
        if latest.talk_seconds == 0:
            logger.warning(
                "talk=0 ham satır alanları: %s",
                {k: raw.get(k) for k in list(raw.keys())[:40]},
            )
        return SearchResult(
            record=latest,
            row_count=len(rows),
            parsed_count=parsed,
            match_count=len(matches),
            sample_keys=sample_keys,
            sample_phone_values=sample_phones,
            meta_summary=meta,
        )

    async def fetch_conversations(
        self, start: date, end: date
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        conversations raporunu çeker.

        Canlı hata: API pageSize=1000 istense bile ~200 satır döndürüp
        total_count=20880 veriyordu; kısa sayfada durmak 400/20880 satırda
        kesiyordu → numara set dışında kalıp BULUNAMADI.

        Strateji: total_count bitene kadar sayfala; büyük pencereleri
        günlük parçala (rate limit + erken arama için).
        """
        chunks = self._date_chunks(start, end, max_days=3)
        all_rows: list[dict[str, Any]] = []
        last_meta: dict[str, Any] = {}
        seen_ids: set[str] = set()
        total_reported = 0
        total_fetched = 0

        for c_start, c_end in chunks:
            rows, meta = await self._fetch_window_paginated(c_start, c_end)
            last_meta = meta
            total_reported += int(meta.get("total_count") or 0)
            total_fetched += int(meta.get("fetched_count") or len(rows))
            for r in rows:
                rid = self._row_dedupe_key(r)
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                all_rows.append(r)

        last_meta = {
            **last_meta,
            "total_count": total_reported or last_meta.get("total_count"),
            "fetched_count": len(all_rows),
            "windows": len(chunks),
        }

        if all_rows and isinstance(all_rows[0], dict) and not self._logged_schema:
            self._logged_schema = True
            logger.info(
                "Toniva conversations örnek alanlar: %s | meta=%s | toplam_satır=%s parçalar=%s",
                list(all_rows[0].keys())[:40],
                {
                    k: last_meta.get(k)
                    for k in ("total_count", "truncated", "page", "page_size", "fetched_count")
                },
                len(all_rows),
                len(chunks),
            )

        return all_rows, last_meta

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

    @staticmethod
    def _row_dedupe_key(row: dict[str, Any]) -> str:
        if not isinstance(row, dict):
            return str(id(row))
        for k in (
            "CallID",
            "callId",
            "call_id",
            "id",
            "uniqueid",
            "uniqueId",
            "linkedid",
            "uuid",
        ):
            if row.get(k) not in (None, ""):
                return f"{k}:{row.get(k)}"
        parts = []
        for k in sorted(row.keys()):
            v = row[k]
            if isinstance(v, (dict, list)):
                continue
            parts.append(f"{k}={v}")
            if len(parts) >= 12:
                break
        return "|".join(parts) if parts else str(id(row))

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

    async def _fetch_window_paginated(
        self, start: date, end: date
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Tek tarih penceresini tamamen çek.

        Kritik: API istenen pageSize'dan az satır döndürebilir (ör. 200).
        total_count varsa kısa sayfada DURMA — total dolana kadar sayfala.
        """
        # OpenAPI max 5000; API fiilen ~200 de kesebilir
        requested_page_size = 5000
        page = 1
        all_rows: list[dict[str, Any]] = []
        total: int | None = None
        last_meta: dict[str, Any] = {}
        effective_page_size: int | None = None

        while True:
            data = await self._get_report(
                {
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "pageSize": requested_page_size,
                    "page": page,
                }
            )
            batch = self._extract_rows(data)
            meta = self._extract_meta(data)
            last_meta = meta
            t = self._meta_total_count(meta)
            if total is None and t is not None:
                total = t

            if page == 1 and not batch:
                data2 = await self._get_report(
                    {
                        "startDate": start.isoformat(),
                        "endDate": end.isoformat(),
                    }
                )
                batch2 = self._extract_rows(data2)
                meta2 = self._extract_meta(data2)
                if batch2:
                    logger.info(
                        "pageSize'sız fallback: rows=%s meta=%s",
                        len(batch2),
                        {k: meta2.get(k) for k in ("total_count", "truncated", "totalCount")},
                    )
                    return batch2, {
                        **meta2,
                        "fetched_count": len(batch2),
                        "window": f"{start.isoformat()}..{end.isoformat()}",
                    }
                if isinstance(data, dict):
                    last_meta = {**meta, "_raw_keys": list(data.keys())[:20]}
                return [], {
                    **last_meta,
                    "fetched_count": 0,
                    "window": f"{start.isoformat()}..{end.isoformat()}",
                }

            if not batch:
                # sonraki sayfa boş → bitti
                break

            all_rows.extend(batch)
            if effective_page_size is None:
                effective_page_size = len(batch)

            logger.info(
                "conversations sayfa=%s batch=%s toplam_çekilen=%s total_count=%s (%s→%s)",
                page,
                len(batch),
                len(all_rows),
                total,
                start,
                end,
            )

            if total is not None and len(all_rows) >= total:
                break

            # total biliniyor ve henüz dolmadı → kısa sayfa olsa bile devam
            if total is not None and len(all_rows) < total:
                page += 1
                if page > 500:
                    logger.warning(
                        "sayfalama 500 sayfada kesildi (%s→%s) çekilen=%s total=%s",
                        start,
                        end,
                        len(all_rows),
                        total,
                    )
                    break
                continue

            # total yok: fiili sayfa boyutundan kısa → son sayfa
            page_cap = effective_page_size or requested_page_size
            if len(batch) < page_cap:
                break

            page += 1
            if page > 500:
                logger.warning(
                    "sayfalama 500 sayfada kesildi (%s→%s) çekilen=%s",
                    start,
                    end,
                    len(all_rows),
                )
                break

        last_meta = {
            **last_meta,
            "total_count": total if total is not None else last_meta.get("total_count"),
            "fetched_count": len(all_rows),
            "pages": page,
            "effective_page_size": effective_page_size,
            "window": f"{start.isoformat()}..{end.isoformat()}",
        }
        if total is not None and len(all_rows) < total:
            logger.warning(
                "eksik çekim: fetched=%s < total_count=%s (%s→%s)",
                len(all_rows),
                total,
                start,
                end,
            )
        return all_rows, last_meta

    async def _get_report(self, params: dict[str, Any]) -> Any:
        url = "/reports/conversations"
        last_err: Exception | None = None
        for attempt in range(6):
            try:
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                logger.exception("Toniva istek hatası: %s", exc)
                raise RuntimeError(f"Toniva API bağlantı hatası: {exc}") from exc

            if resp.status_code == 429:
                raw_retry = resp.headers.get("Retry-After", "2")
                try:
                    wait_s = max(1, int(float(raw_retry)))
                except ValueError:
                    wait_s = 2
                wait_s = min(wait_s, 30)
                logger.warning(
                    "Toniva 429 rate limit, %ss bekleniyor (deneme %s/6)",
                    wait_s,
                    attempt + 1,
                )
                await asyncio.sleep(wait_s)
                last_err = RuntimeError(
                    f"Toniva rate limit (CRM-2094). Retry-After: {raw_retry} sn"
                )
                continue

            if resp.status_code >= 400:
                detail = resp.text[:400]
                raise RuntimeError(
                    f"Toniva API hata {resp.status_code}: {detail}"
                )

            try:
                return resp.json()
            except ValueError as exc:
                raise RuntimeError("Toniva API geçersiz JSON döndü") from exc

        raise last_err or RuntimeError("Toniva rate limit aşıldı")

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

        # Derin arama: en büyük dict-listesini satır kabul et
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
    def _normalize_row_list(
        rows: list[Any],
        columns: Any,
    ) -> list[dict[str, Any]]:
        """Dict satırlar + [col...] / list satır birleşimi."""
        col_names: list[str] | None = None
        if isinstance(columns, list) and columns:
            col_names = [
                TonivaClient._column_to_name(c) for c in columns
            ]
        elif isinstance(columns, dict):
            # {0: "phone", 1: "date"} veya {"fields": [...]}
            if "fields" in columns and isinstance(columns["fields"], list):
                col_names = [
                    TonivaClient._column_to_name(c) for c in columns["fields"]
                ]

        out: list[dict[str, Any]] = []
        for r in rows:
            if isinstance(r, dict):
                out.append(r)
                continue
            if isinstance(r, (list, tuple)) and col_names:
                n = min(len(col_names), len(r))
                out.append({col_names[i]: r[i] for i in range(n)})
        return out

    @staticmethod
    def _column_to_name(col: Any) -> str:
        """columns: 'TELEFON' | {key/label/name/field: ...}"""
        if isinstance(col, dict):
            for k in ("key", "name", "field", "label", "id", "slug"):
                v = col.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
            return str(col)
        return str(col)

    @classmethod
    def _flatten_row(cls, row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        """İç içe dict'leri tek düzeye indir (stats.talk → stats_talk / talk)."""
        flat: dict[str, Any] = {}
        for k, v in row.items():
            key = f"{prefix}{k}" if not prefix else f"{prefix}_{k}"
            if isinstance(v, dict):
                flat[key] = v  # üst anahtar da dursun
                nested = cls._flatten_row(v, prefix=str(k))
                flat.update(nested)
                # kısa adlar: iç key'ler üstte de erişilebilir olsun
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

    def _parse_row(self, row: dict[str, Any]) -> CallRecord | None:
        phone_raw = self._extract_external_phone(row)
        if phone_raw is None:
            # nested common shapes
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
            agent = self._pick(
                row, ("cnam", "CNAM", "agentname", "memberName", "member_name")
            )
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
    def _row_contains_phone(cls, row: dict[str, Any], target: str) -> bool:
        """Satırdaki tüm skaler değerlerde hedef numarayı ara."""
        for val in row.values():
            if val is None or isinstance(val, (dict, list, bool)):
                continue
            if phones_equal(str(val), target):
                return True
            # "905466033161;605" veya "from 905466033161" gibi birleşik alanlar
            s = str(val)
            d = digits_only(s)
            if len(d) >= 10 and phones_equal(d[-10:], target):
                return True
            # metin içinde 10+ haneli adaylar
            for m in re.finditer(r"\d{10,14}", d if d else s):
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
            looks = (
                len(d) >= 7
                or cls._key_looks_like_phone_field(fk)
                or cls._is_extension_key(fk)
            )
            if looks:
                out.append(f"{key}={s[:32]}")
            if len(out) >= limit:
                break
        return out

    @classmethod
    def _extract_external_phone(cls, row: dict[str, Any]) -> Any | None:
        """
        Müşteri / harici telefonu seç.

        Canlı hata: API bazen phone/number/src = dahili (605), dst/telefon = 9054...
        Eski _pick sırası dahiliyi alıp /kt 9054... için BULUNAMADI üretiyordu.
        """
        candidates: list[tuple[int, int, Any]] = []
        # (score, key_priority, value) — score yüksek, key_priority düşük = daha iyi

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

        # 1) Bilinen dış-numara anahtarları
        for i, k in enumerate(_PHONE_KEYS):
            raw = cls._pick(row, (k,))
            if raw is not None:
                add(raw, key_name=k, key_priority=i)

        # 2) Satırdaki telefon-benzeri tüm alanlar (Telefon Numarası vb.)
        for key, val in row.items():
            if val is None or val == "" or isinstance(val, (dict, list)):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_extension_key(fk):
                add(val, key_name=str(key), key_priority=500)
                continue
            if not cls._key_looks_like_phone_field(fk):
                continue
            # zaten _PHONE_KEYS ile alındıysa tekrar eklemek skorlamada zararsız
            add(val, key_name=str(key), key_priority=100)

        # 3) Dahili alanlar — yedek
        for i, k in enumerate(_EXTENSION_PHONE_KEYS):
            raw = cls._pick(row, (k,))
            if raw is not None:
                add(raw, key_name=k, key_priority=1000 + i)

        # 4) Hiç güçlü dış aday yoksa: tüm skaler değerlerde TR numara ara
        if not any(score >= 50 for score, _, _ in candidates):
            for key, val in row.items():
                if val is None or val == "" or isinstance(val, (dict, list, bool)):
                    continue
                s = str(val).strip()
                if not s or s in ("-", "—", "–"):
                    continue
                if normalize_tr_phone(s):
                    add(s, key_name=str(key), key_priority=200)

        if not candidates:
            return None

        # En yüksek skor; eşitlikte key_priority (daha erken / dış alan)
        best = max(candidates, key=lambda t: (t[0], t[1]))
        return best[2]

    @staticmethod
    def _is_extension_key(folded_key: str) -> bool:
        fk = folded_key
        if any(x in fk for x in ("dahili", "extension", "exten")):
            # "external" false positive olmasın
            if "external" in fk:
                return False
            return True
        if fk in ("ext", "src") or fk.endswith("ext"):
            return True
        return False

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
        if "numara" in fk and "dahili" not in fk:
            return True
        return False

    @classmethod
    def _phone_value_score(cls, value: str, *, key_name: str) -> int:
        """Yüksek = dış hat / mobil; düşük = dahili."""
        d = re.sub(r"\D+", "", value)
        if not d:
            return -1

        fk = cls._fold_key(key_name)
        ext_key = cls._is_extension_key(fk)

        # TR normalize başarılı (90 + 10 hane, genelde 5xx)
        norm = normalize_tr_phone(value)
        if norm and len(norm) >= 12:
            base = 100
            # 5 ile başlayan mobil gövde
            if norm[2:3] == "5":
                base = 120
            if ext_key:
                return 20  # anahtar dahili ama değer uzun numara — şüpheli, düşük
            return base

        # 10+ hane (ülke kodu olmadan veya kirli format)
        if len(d) >= 10:
            if ext_key:
                return 25
            if d[-10:].startswith("5"):
                return 90
            return 70

        # 7–9 hane: zayıf aday
        if len(d) >= 7:
            return 30 if not ext_key else 10

        # 3–6 hane: neredeyse kesin dahili (605 vb.)
        if 3 <= len(d) <= 6:
            return 5 if ext_key else 8

        return -1

    @classmethod
    def _extract_datetime(cls, row: dict[str, Any]) -> tuple[datetime, str, str]:
        """
        Satırdan tarih+saat çıkar.

        Canlı hata:
        - `calldate` / `date` yalnızca gün bilgisini taşıyıp 00:00:00 dönüyordu;
          paneldeki SAAT ayrı alanda kalıyordu.
        - `time: 0` süre/flag; saat değildir.
        """
        clock_raw = cls._find_clock_value(row)

        best: tuple[datetime, str, str] | None = None

        # 1) Bilinen datetime alanları
        for key in _DATETIME_KEYS:
            raw = cls._pick(row, (key,))
            if raw is None:
                continue
            parsed = cls._try_parse_single_datetime(raw)
            if parsed is None:
                continue
            merged = cls._apply_clock_if_needed(parsed, clock_raw)
            best = cls._prefer_datetime(best, merged)

        # 2) Ayrı tarih + saat
        date_raw = cls._pick(row, _DATE_ONLY_KEYS)
        if date_raw is not None:
            resolved = cls._resolve_datetime(date_raw, clock_raw)
            if resolved[0] != datetime.min:
                best = cls._prefer_datetime(best, resolved)

        # 3) Heuristik tarama
        for key, val in row.items():
            if val in (None, "", 0, "0"):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_duration_like_key(fk):
                continue
            if not any(
                x in fk
                for x in (
                    "date",
                    "time",
                    "tarih",
                    "saat",
                    "start",
                    "created",
                    "call",
                    "ts",
                    "stamp",
                )
            ):
                continue
            parsed = cls._try_parse_single_datetime(val)
            if parsed is not None:
                merged = cls._apply_clock_if_needed(parsed, clock_raw)
                best = cls._prefer_datetime(best, merged)
                continue
            if isinstance(val, str) and _parse_date_only(val.strip()):
                resolved = cls._resolve_datetime(val, clock_raw)
                if resolved[0] != datetime.min:
                    best = cls._prefer_datetime(best, resolved)

        if best is not None:
            return best

        return cls._resolve_datetime(date_raw, clock_raw)

    @staticmethod
    def _is_duration_like_key(folded_key: str) -> bool:
        fk = folded_key
        # Canlı şema: CallTime/RingTime/WaitTime süre; CreateTime duvar saati
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
    def _find_clock_value(cls, row: dict[str, Any]) -> Any:
        """Ayrı saat alanını bul (süre/flag olan 0 değerlerini ele)."""
        for k in _TIME_ONLY_KEYS:
            if cls._is_duration_like_key(cls._fold_key(k)):
                continue  # CallTime = görüşme süresi, saat değil
            raw = cls._pick(row, (k,))
            if raw is None:
                continue
            if cls._is_plausible_clock(raw):
                return raw

        # Heuristik: anahtar adında saat/time, değerde HH:MM:SS
        for key, val in row.items():
            if val in (None, "", 0, "0"):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_duration_like_key(fk):
                continue
            if not any(x in fk for x in ("saat", "time", "clock", "hour")):
                continue
            # "timestamp", "datetime" birleşik alan — saat değil
            if any(x in fk for x in ("stamp", "date", "duration", "starttime", "started")):
                # startTime birleşik olabilir; değer yalnızca saat formatındaysa al
                if not (
                    isinstance(val, str)
                    and re.fullmatch(r"\d{1,2}[:.]\d{2}([:.]\d{2})?", val.strip())
                ):
                    continue
            if cls._is_plausible_clock(val):
                return val
        return None

    @classmethod
    def _apply_clock_if_needed(
        cls,
        parsed: tuple[datetime, str, str],
        clock_raw: Any,
    ) -> tuple[datetime, str, str]:
        """Datetime gece yarısı ise (veya saatsiz) ayrı clock ile birleştir."""
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
        """Gerçek saati olan adayı (00:00:00 olmayan) tercih et."""
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
        # ikisi de saati dolu veya ikisi de gece yarısı → daha geç olan
        return candidate if n_dt >= c_dt else current

    @classmethod
    def _clock_to_str(cls, raw: Any) -> str:
        if isinstance(raw, str):
            return raw.strip().replace(".", ":")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            n = int(raw)
            # HHMMSS (ör. 214548 → 21:45:48)
            if 0 < n <= 235959:
                s = f"{n:06d}"
                return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"
        return str(raw).strip()

    @classmethod
    def _is_plausible_clock(cls, raw: Any) -> bool:
        if raw is None or raw == "":
            return False
        if isinstance(raw, bool):
            return False
        if isinstance(raw, (int, float)):
            n = int(raw)
            # 0 = süre/flag; HHMMSS (en az 00:01:00 → 100) kabul
            if n <= 0:
                return False
            if n <= 235959:
                s = f"{n:06d}"
                hh, mm, ss = int(s[0:2]), int(s[2:4]), int(s[4:6])
                return hh <= 23 and mm <= 59 and ss <= 59
            return False
        s = str(raw).strip().replace(".", ":")
        if not s or s in ("0", "0.0", "-", "—", "–"):
            return False
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
            return True
        return False

    @classmethod
    def _try_parse_single_datetime(cls, raw: Any) -> tuple[datetime, str, str] | None:
        if raw is None or raw == "" or raw in ("-", "—", "–"):
            return None

        if isinstance(raw, bool):
            return None

        if isinstance(raw, datetime):
            dt = raw.replace(tzinfo=None) if raw.tzinfo else raw
            return dt, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")

        if isinstance(raw, date) and not isinstance(raw, datetime):
            dt = datetime.combine(raw, time(0, 0, 0))
            return dt, dt.strftime("%d.%m.%Y"), "00:00:00"

        if isinstance(raw, (int, float)):
            ts = float(raw)
            # çok küçük sayılar süre/flag — datetime değil
            if ts < 1_000_000_000:  # ~2001 öncesi epoch altı eşiği
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

        # Yalnızca saat string'i tek başına datetime sayılmaz
        if re.fullmatch(r"\d{1,2}[:.]\d{2}([:.]\d{2})?", s):
            return None

        resolved = cls._resolve_datetime(s, None)
        if resolved[0] != datetime.min:
            return resolved
        return None
    @staticmethod
    def _fold_key(key: str) -> str:
        """Türkçe karakterleri sadeleştirerek karşılaştırma anahtarı üret."""
        s = str(key).strip().lower().replace(" ", "").replace("_", "")
        # 'i' + combining dot (İ.tolower) çok karakterli olabilir → replace
        s = s.replace("i̇", "i").replace("ı", "i")
        for src, dst in (
            ("ğ", "g"),
            ("ü", "u"),
            ("ş", "s"),
            ("ö", "o"),
            ("ç", "c"),
        ):
            s = s.replace(src, dst)
        return s

    @classmethod
    def _pick(cls, row: dict[str, Any], keys: tuple[str, ...]) -> Any:
        # exact
        for k in keys:
            if k in row and row[k] not in (None, ""):
                return row[k]
        # case / TR insensitive
        folded_map = {
            cls._fold_key(k): v for k, v in row.items() if v not in (None, "")
        }
        for k in keys:
            v = folded_map.get(cls._fold_key(k))
            if v not in (None, ""):
                return v
        return None

    @classmethod
    def _pick_all_present(cls, row: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
        """Anahtar listesindeki tüm mevcut değerler (0 dahil; boş string hariç)."""
        found: list[Any] = []
        seen: set[int] = set()
        folded_map = {cls._fold_key(k): v for k, v in row.items()}
        for k in keys:
            if k in row and row[k] not in (None, ""):
                vid = id(row[k])
                if vid not in seen:
                    seen.add(vid)
                    found.append(row[k])
                continue
            v = folded_map.get(cls._fold_key(k))
            if v not in (None, ""):
                vid = id(v)
                if vid not in seen:
                    seen.add(vid)
                    found.append(v)
        return found

    @classmethod
    def _extract_talk_seconds(cls, row: dict[str, Any]) -> int:
        """
        Panel GÖRÜŞME SÜRESİ (sn).

        Canlı hatalar:
        - 00:09:51 → total/max şişirmesi
        - 00:00:01 → satırdaki rastgele int(1) süre sanılıp min() ile seçildi

        Kural (sıkı):
        1) Yalnızca SÜRE anahtarı olan alanlar (talk/billsec/gorusme/…)
        2) Bilinmeyen alandaki çıplak int ASLA süre değildir
        3) min(tüm adaylar) YASAK — talk alanlarından max
        4) total−ring yalnızca named total + named ring
        """
        talk = cls._seconds_from_named_fields(row, kind="talk")
        if talk > 0:
            return talk

        ring = cls._seconds_from_named_fields(row, kind="ring")
        total = cls._seconds_from_named_fields(row, kind="total")

        if total > 0 and ring > 0 and total > ring:
            derived = total - ring
            # kuyruk+bekleme şişirmesi (591 sn vb.)
            if derived > 300 and derived > max(ring, 1) * 20:
                logger.info(
                    "total-ring atlandı total=%s ring=%s derived=%s",
                    total,
                    ring,
                    derived,
                )
            else:
                return derived

        # duration tek başına bazen talk (billsec yok)
        if total > 0 and ring == 0 and total <= 3600:
            return total

        return 0

    @classmethod
    def _seconds_from_named_fields(cls, row: dict[str, Any], *, kind: str) -> int:
        """
        kind: talk | ring | total
        Sadece anahtar adı süre anlamı taşıyorsa değer okunur.
        """
        best = 0
        for key, val in row.items():
            if val in (None, "", "-", "—", "–"):
                continue
            fk = cls._fold_key(str(key))
            if not cls._key_matches_duration_kind(fk, kind):
                continue
            # duvar saati alanları
            if fk in ("time", "saat", "date", "tarih") or fk.endswith("clock"):
                continue
            if "timestamp" in fk or (fk.endswith("at") and "duration" not in fk and "sec" not in fk):
                continue
            sec = cls._parse_duration_seconds(val)
            # talk/ring için 00:MM:SS veya makul sn; 21:56:59 gibi saatleri ele
            if isinstance(val, str) and re.fullmatch(
                r"\d{1,2}:\d{2}:\d{2}",
                val.strip().replace(".", ":").replace("：", ":"),
            ):
                parts = [int(p) for p in val.strip().replace(".", ":").replace("：", ":").split(":")]
                if parts[0] >= 3:  # duvar saati
                    continue
            if sec <= 0 or sec > 6 * 3600:
                continue
            best = max(best, sec)
        return best

    @classmethod
    def _key_matches_duration_kind(cls, folded_key: str, kind: str) -> bool:
        fk = folded_key
        # recording ≠ ring
        if "record" in fk and kind == "ring":
            return False

        if kind == "ring":
            if any(x in fk for x in ("ring", "caldir", "ringing")):
                return "record" not in fk
            return False

        if kind == "talk":
            # Canlı Toniva: CallTime = görüşme süresi (CreateTime ≠)
            if fk in ("calltime", "call_time"):
                return True
            # Net görüşme alanları — "answer"/"active" yok (şişirme)
            if any(
                x in fk
                for x in (
                    "gorusme",
                    "talkduration",
                    "talk_duration",
                    "talksec",
                    "talkseconds",
                    "talktime",
                    "billsec",
                    "billedseconds",
                    "conversationduration",
                    "connectedduration",
                    "bridgeduration",
                    "speakingduration",
                    "speakingtime",
                )
            ):
                return True
            # talk + (duration|sec|sure|time) ama ring değil
            if "talk" in fk and any(x in fk for x in ("duration", "sec", "sure", "time")):
                return not cls._key_matches_duration_kind(fk, "ring")
            if "bill" in fk and "sec" in fk:
                return True
            if "gorusme" in fk:
                return True
            return False

        if kind == "total":
            # yalnızca genel çağrı süresi — queue/hold/wait hariç
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

        return False

    @staticmethod
    def _is_ring_like_key(folded_key: str) -> bool:
        if "record" in folded_key:
            return False
        return any(
            x in folded_key
            for x in ("ring", "caldir", "ringing")
        )

    @staticmethod
    def _resolve_datetime(
        date_raw: Any,
        time_raw: Any,
    ) -> tuple[datetime, str, str]:
        """Tarih/saat alanlarını parse et; gösterim + sıralama anahtarı üret."""
        fallback = datetime.min

        # date_raw ISO datetime ise
        if isinstance(date_raw, (int, float)):
            # epoch sn / ms
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

        # "2026-07-18T21:58:47" veya "2026-07-18 21:58:47"
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
                    return (
                        dt,
                        dt.strftime("%d.%m.%Y"),
                        dt.strftime("%H:%M:%S"),
                    )
                except ValueError:
                    continue
            # fromisoformat (milisaniye / offset)
            try:
                dt = datetime.fromisoformat(candidate)
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
                return dt, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")
            except ValueError:
                pass

        # Ayrı tarih + saat
        d_part = _parse_date_only(date_str)
        t_part = _parse_time_only(time_str) if time_str else None

        if d_part is not None:
            t_use = t_part or time(0, 0, 0)
            dt = datetime.combine(d_part, t_use)
            date_disp = d_part.strftime("%d.%m.%Y")
            time_disp = t_use.strftime("%H:%M:%S") if t_part else (time_str or "00:00:00")
            return dt, date_disp, time_disp

        # UI tarzı: "Cumartesi 18 Temmuz 2026"
        parsed_ui = _parse_turkish_long_date(date_str)
        if parsed_ui is not None:
            t_use = _parse_time_only(time_str) or time(0, 0, 0)
            dt = datetime.combine(parsed_ui, t_use)
            time_disp = (
                t_use.strftime("%H:%M:%S")
                if _parse_time_only(time_str)
                else (time_str or "00:00:00")
            )
            return dt, parsed_ui.strftime("%d.%m.%Y"), time_disp

        # Son çare: ham metin, sıralama zayıf
        return (
            fallback,
            date_str or "—",
            time_str or "—",
        )

    @staticmethod
    def _parse_duration_seconds(raw: Any) -> int:
        """Süreyi saniyeye çevir (görüşme veya ring)."""
        if raw is None or raw == "" or raw in ("-", "—", "–"):
            return 0
        if isinstance(raw, bool):
            return 0
        if isinstance(raw, (int, float)):
            n = float(raw)
            if n <= 0:
                return 0
            # milisaniye (ör. 86000 → 86 sn)
            if n > 6 * 3600:
                return max(0, int(round(n / 1000.0)))
            return int(n)

        s = str(raw).strip().replace("：", ":")
        if not s:
            return 0

        # HH:MM:SS veya MM:SS (nokta ayırıcı da)
        s_norm = s.replace(".", ":") if re.fullmatch(r"\d{1,2}[.:]\d{2}([.:]\d{2})?", s) else s
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s_norm):
            parts = [int(p) for p in s_norm.split(":")]
            if len(parts) == 3:
                h, m, sec = parts
                return max(0, h * 3600 + m * 60 + sec)
            if len(parts) == 2:
                m, sec = parts
                return max(0, m * 60 + sec)

        # "11 sn", "11s", "11 saniye", "1 dk 26 sn"
        dk = re.search(r"(\d+)\s*(?:dk|dakika|min)", s, re.I)
        sn = re.search(r"(\d+)\s*(?:sn|sec|saniye|s)\b", s, re.I)
        if dk or sn:
            total = 0
            if dk:
                total += int(dk.group(1)) * 60
            if sn:
                total += int(sn.group(1))
            if total > 0:
                return total

        m = re.search(r"(\d+)", s)
        if m and re.search(r"sn|sec|saniye", s, re.I):
            return max(0, int(m.group(1)))

        if re.fullmatch(r"\d+", s):
            return TonivaClient._parse_duration_seconds(int(s))

        return 0


def digits_keep(value: str) -> str:
    return re.sub(r"\D+", "", value) or value


_TR_MONTHS = {
    "ocak": 1,
    "şubat": 2,
    "subat": 2,
    "mart": 3,
    "nisan": 4,
    "mayıs": 5,
    "mayis": 5,
    "haziran": 6,
    "temmuz": 7,
    "ağustos": 8,
    "agustos": 8,
    "eylül": 9,
    "eylul": 9,
    "ekim": 10,
    "kasım": 11,
    "kasim": 11,
    "aralık": 12,
    "aralik": 12,
}


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
    """Örn: 'Cumartesi 18 Temmuz 2026'"""
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
    # Türkçe İ/i normalizasyonu
    month_name = (
        month_name.replace("ı", "i")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ş", "s")
        .replace("ö", "o")
        .replace("ç", "c")
    )
    # map with ascii-folded keys too
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
    month = _TR_MONTHS.get(m.group(2).lower()) or folded.get(month_name)
    if not month:
        return None
    year = int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None
