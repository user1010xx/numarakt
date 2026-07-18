"""Toniva Public API — görüşme raporu (conversations) istemcisi."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import httpx

from phone_utils import normalize_tr_phone, phones_equal

logger = logging.getLogger(__name__)

# UI / API alan adı varyasyonları
_AGENT_KEYS = (
    "dahiliAdi",
    "dahili_adi",
    "DAHİLİ ADI",
    "Dahili Adı",
    "agentName",
    "agent_name",
    "extensionName",
    "extension_name",
    "userName",
    "user_name",
    "agent",
    "personel",
    "personelAdi",
    "personel_adi",
    "name",
    "dahili",
)
_PHONE_KEYS = (
    "telefon",
    "TELEFON",
    "Telefon",
    "phone",
    "phoneNumber",
    "phone_number",
    "caller",
    "callerNumber",
    "caller_number",
    "callee",
    "calleeNumber",
    "callee_number",
    "number",
    "msisdn",
    "externalNumber",
    "external_number",
    "dst",
    "src",
)
# Tam tarih+saat veya yalnızca tarih taşıyabilen alanlar (öncelik sırası)
_DATETIME_KEYS = (
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
    "tarih",
    "TARİH",
    "Tarih",
    "date",
    "day",
    "callDay",
    "call_day",
)
_TIME_ONLY_KEYS = (
    "saat",
    "SAAT",
    "Saat",
    "callTime",
    "call_time",
    "callStartTime",
    "call_start_time",
    "startClock",
    "timeOfDay",
    "time_of_day",
    "clock",
    "hour",
    "hours",
    # bare "time" en sonda — API bazen time=0 (süre) gönderiyor, saat değil
    "time",
)
# Görüşme (talk) — çaldırma/ring YOK. Önce spesifik, sonda genel.
_TALK_DURATION_KEYS = (
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
    "ringTime",
    "ring_time",
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
    ) -> CallRecord | None:
        """Son 30 gün (veya verilen aralık) içinde numaraya ait en son kaydı bul."""
        target = normalize_tr_phone(phone) or phone
        rows = await self.fetch_conversations(start, end)

        if not rows:
            logger.info(
                "conversations boş döndü (%s → %s)",
                start.isoformat(),
                end.isoformat(),
            )
            return None

        matches: list[tuple[CallRecord, dict[str, Any]]] = []
        parsed = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            flat = self._flatten_row(row)
            rec = self._parse_row(flat)
            if rec is None:
                continue
            parsed += 1
            if phones_equal(rec.phone, target):
                matches.append((rec, flat))

        if parsed == 0:
            sample = rows[0] if rows else {}
            if isinstance(sample, dict):
                keys = list(self._flatten_row(sample).keys())
            else:
                keys = [f"row_type={type(sample).__name__}"]
            raise RuntimeError(
                "Toniva satırları geldi ama telefon alanı okunamadı. "
                f"Örnek alan adları: {keys[:30]}. "
                "toniva_client alan eşlemesi güncellenmeli."
            )

        if not matches:
            logger.info(
                "Numara eşleşmedi: target=%s satır=%s parse=%s",
                target,
                len(rows),
                parsed,
            )
            return None

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
            # Railway log — alan adını net görmek için
            logger.warning(
                "talk=0 ham satır alanları: %s",
                {k: raw.get(k) for k in list(raw.keys())[:40]},
            )
        return latest

    async def fetch_conversations(self, start: date, end: date) -> list[dict[str, Any]]:
        """
        conversations raporunu çeker.

        OpenAPI: pageSize yoksa penceredeki tüm satırlar (max 5000).
        truncated ise pageSize ile sayfalar.
        """
        params: dict[str, Any] = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }
        data = await self._get_report(params)
        rows = self._extract_rows(data)
        meta = self._extract_meta(data)

        if meta.get("truncated") or (
            isinstance(meta.get("total_count"), int)
            and len(rows) < int(meta["total_count"])
        ):
            rows = await self._fetch_all_pages(start, end, meta)

        if rows and isinstance(rows[0], dict) and not self._logged_schema:
            self._logged_schema = True
            logger.info(
                "Toniva conversations örnek alanlar: %s | meta=%s",
                list(rows[0].keys()),
                {k: meta.get(k) for k in ("total_count", "truncated", "page", "page_size")},
            )

        return rows

    async def _fetch_all_pages(
        self,
        start: date,
        end: date,
        first_meta: dict[str, Any],
    ) -> list[dict[str, Any]]:
        page_size = 1000
        page = 1
        all_rows: list[dict[str, Any]] = []
        total = first_meta.get("total_count")

        while True:
            data = await self._get_report(
                {
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "pageSize": page_size,
                    "page": page,
                }
            )
            batch = self._extract_rows(data)
            if not batch:
                break
            all_rows.extend(batch)
            meta = self._extract_meta(data)
            if total is None:
                total = meta.get("total_count")
            if total is not None and len(all_rows) >= int(total):
                break
            if len(batch) < page_size:
                break
            page += 1
            if page > 50:  # güvenlik
                logger.warning("conversations sayfalama 50 sayfada kesildi")
                break

        return all_rows

    async def _get_report(self, params: dict[str, Any]) -> Any:
        url = "/reports/conversations"
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            logger.exception("Toniva istek hatası: %s", exc)
            raise RuntimeError(f"Toniva API bağlantı hatası: {exc}") from exc

        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After", "?")
            raise RuntimeError(
                f"Toniva rate limit (CRM-2094). Retry-After: {retry} sn"
            )

        if resp.status_code >= 400:
            detail = resp.text[:400]
            raise RuntimeError(
                f"Toniva API hata {resp.status_code}: {detail}"
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError("Toniva API geçersiz JSON döndü") from exc

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

        for key in ("rows", "data", "items", "results", "records", "conversations"):
            val = data.get(key)
            if isinstance(val, list):
                return TonivaClient._normalize_row_list(val, columns)
            if isinstance(val, dict):
                for inner in ("rows", "data", "items", "results"):
                    if isinstance(val.get(inner), list):
                        return TonivaClient._normalize_row_list(val[inner], columns)

        report = data.get("report")
        if isinstance(report, dict):
            return TonivaClient._extract_rows(report)

        return []

    @staticmethod
    def _normalize_row_list(
        rows: list[Any],
        columns: Any,
    ) -> list[dict[str, Any]]:
        """Dict satırlar + [col...] / list satır birleşimi."""
        col_names: list[str] | None = None
        if isinstance(columns, list) and columns:
            col_names = [str(c) for c in columns]
        elif isinstance(columns, dict):
            # {0: "phone", 1: "date"} veya {"fields": [...]}
            if "fields" in columns and isinstance(columns["fields"], list):
                col_names = [str(c) for c in columns["fields"]]

        out: list[dict[str, Any]] = []
        for r in rows:
            if isinstance(r, dict):
                out.append(r)
                continue
            if isinstance(r, (list, tuple)) and col_names:
                n = min(len(col_names), len(r))
                out.append({col_names[i]: r[i] for i in range(n)})
        return out

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
        phone_raw = self._pick(row, _PHONE_KEYS)
        if phone_raw is None:
            # nested common shapes
            for nest in ("party", "remote", "customer", "contact"):
                nested = row.get(nest)
                if isinstance(nested, dict):
                    phone_raw = self._pick(nested, _PHONE_KEYS)
                    if phone_raw is not None:
                        break

        if phone_raw is None:
            return None

        phone_str = str(phone_raw).strip()
        if not phone_str or phone_str in ("-", "—", "–"):
            return None

        agent = self._pick(row, _AGENT_KEYS)
        if agent is None:
            # FreePBX / CDR
            agent = self._pick(row, ("cnam", "CNAM", "agentname", "memberName", "member_name"))
        agent_str = str(agent).strip() if agent is not None else "—"
        if not agent_str:
            agent_str = "—"

        talk_seconds = self._extract_talk_seconds(row)

        sort_dt, date_disp, time_disp = self._extract_datetime(row)

        display_phone = normalize_tr_phone(phone_str) or digits_keep(phone_str)

        return CallRecord(
            agent_name=agent_str,
            phone=display_phone,
            call_date=date_disp,
            call_time=time_disp,
            talk_seconds=talk_seconds,
            sort_key=sort_dt,
        )

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
        return any(
            x in folded_key
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
        Görüşme süresi (sn) = panel GÖRÜŞME SÜRESİ.

        Canlı hata (00:09:51 yerine 00:01:26 olmalı):
        - total−ring veya max(tüm süreler) şişirilmiş değer seçiyordu
          (591 sn = muhtemelen arama başlangıcı→bitiş; gerçek talk 86 sn).
        - Kör taramada max() almak total'i talk sanıyordu.

        Kural: açık talk alanı > kısa 00:MM:SS adayları (min) > dikkatli total−ring.
        """
        ring = cls._collect_ring_seconds(row)
        talk_explicit = cls._collect_explicit_talk_seconds(row)
        if talk_explicit > 0:
            return talk_explicit

        # Açık total alanları (sadece bilinen key'ler)
        total = 0
        for raw in cls._pick_all_present(row, _TOTAL_DURATION_KEYS):
            sec = cls._parse_duration_seconds(raw)
            if 0 < sec <= 3600:
                total = max(total, sec)

        # 00:MM:SS adayları — kaynak ayrımı: talk-ish / total-ish / other
        short_talkish, short_other = cls._collect_short_duration_groups(row, ring)
        all_short = sorted(set(short_talkish + short_other + ([total] if total else [])))

        # 1) Kısa talk-ish adaylar (gorusme/talk string değerleri key'siz tarama)
        if short_talkish:
            return min(short_talkish)

        # 2) ring biliniyor + birden fazla diğer kısa süre: en küçük = görüşme
        #    (86 talk vs 591 total → 86). Tek diğer değer total ise total−ring.
        if ring > 0:
            non_ring = sorted({s for s in all_short if abs(s - ring) > 1})
            if len(non_ring) >= 2:
                return min(non_ring)
            if len(non_ring) == 1:
                only = non_ring[0]
                # only açık total ile aynıysa → total−ring
                if total > 0 and abs(only - total) <= 1 and only > ring:
                    derived = only - ring
                    if derived <= 300 or derived <= ring * 20:
                        return derived
                # only talk kolonu (86) — total değil
                if total == 0 or abs(only - total) > 1:
                    return only

        # 3) total−ring (şişman türevi atla)
        if total > 0 and ring > 0 and total > ring:
            derived = total - ring
            if derived > 300 and derived > ring * 20:
                logger.info(
                    "total-ring şüpheli atlandı: total=%s ring=%s derived=%s",
                    total,
                    ring,
                    derived,
                )
            else:
                return derived
        if total > 0 and ring == 0 and total <= 300:
            return total

        # 4) ring yok, kısa adaylar
        if ring == 0 and all_short:
            if len(all_short) == 1:
                return all_short[0]
            if len(all_short) == 2:
                return max(all_short)
            return all_short[len(all_short) // 2]

        from_ts = cls._talk_from_timestamps(row)
        if 0 < from_ts <= 300:
            return from_ts
        if from_ts > 300:
            logger.info("timestamp talk şüpheli atlandı: %ss", from_ts)

        return 0

    @classmethod
    def _collect_short_duration_groups(
        cls,
        row: dict[str, Any],
        ring: int,
    ) -> tuple[list[int], list[int]]:
        """(talk-ish kısa süreler, diğer kısa süreler)."""
        talkish: list[int] = []
        other: list[int] = []
        skip_key_parts = (
            "phone",
            "telefon",
            "caller",
            "callee",
            "agent",
            "dahili",
            "name",
            "queue",
            "hat",
            "trunk",
            "unique",
            "id",
            "date",
            "tarih",
            "saat",
            "stamp",
        )
        talk_parts = (
            "gorusme",
            "talk",
            "billsec",
            "billed",
            "conversation",
            "connected",
            "bridge",
            "speaking",
        )
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if any(x in fk for x in skip_key_parts):
                if not any(x in fk for x in ("talk", "ring", "bill", "gorusme", "duration", "sure")):
                    continue
            # düz "time" / "saat" duvar saati
            if fk in ("time", "saat") or fk.endswith("clock"):
                continue
            if cls._is_ring_like_key(fk):
                continue
            if not (
                cls._is_panel_duration_string(val)
                or (
                    isinstance(val, (int, float))
                    and not isinstance(val, bool)
                    and 0 < float(val) <= 3600
                )
            ):
                continue
            sec = cls._parse_duration_seconds(val)
            if sec <= 0 or sec >= 3 * 3600:
                continue
            if abs(sec - ring) <= 1 and ring > 0:
                continue
            if any(t in fk for t in talk_parts):
                talkish.append(sec)
            else:
                other.append(sec)
        return talkish, other

    @classmethod
    def _collect_ring_seconds(cls, row: dict[str, Any]) -> int:
        best = 0
        for raw in cls._pick_all_present(row, _RING_DURATION_KEYS):
            best = max(best, cls._parse_duration_seconds(raw))
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_ring_like_key(fk) and cls._looks_like_duration_value(val):
                best = max(best, cls._parse_duration_seconds(val))
        return best

    @classmethod
    def _collect_explicit_talk_seconds(cls, row: dict[str, Any]) -> int:
        """Yalnızca net görüşme alanları (answer/active/service YOK — şişirme riski)."""
        best = 0
        for raw in cls._pick_all_present(row, _TALK_DURATION_KEYS):
            sec = cls._parse_duration_seconds(raw)
            if sec > 0:
                best = max(best, sec)

        strict_hints = (
            "gorusme",
            "talkduration",
            "talk_duration",
            "talksec",
            "talktime",
            "billsec",
            "billed",
            "conversationduration",
            "connectedduration",
            "bridgeduration",
            "speaking",
        )
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_ring_like_key(fk):
                continue
            # timestamp alanlarını ele
            if fk.endswith("at") or "timestamp" in fk or fk in ("time", "saat", "date", "tarih"):
                continue
            if not any(h in fk for h in strict_hints) and "gorusme" not in fk:
                # talk + duration birlikte
                if not ("talk" in fk and ("duration" in fk or "sec" in fk or "sure" in fk)):
                    if not ("bill" in fk and "sec" in fk):
                        continue
            if not cls._looks_like_duration_value(val):
                continue
            sec = cls._parse_duration_seconds(val)
            # talkTime yanlışlıkla duvar saati 21:56:59 olmasın
            if sec >= 3 * 3600:
                continue
            if sec > 0:
                best = max(best, sec)
        return best

    @classmethod
    def _is_panel_duration_string(cls, raw: Any) -> bool:
        """Panel GÖRÜŞME/ÇALDIRMA: 00:01:26 — duvar saati 21:56:59 değil."""
        if not isinstance(raw, str):
            return False
        s = raw.strip().replace("：", ":").replace(".", ":")
        if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
            return False
        parts = [int(p) for p in s.split(":")]
        if len(parts) == 3:
            h, m, sec = parts
            # Panel süreleri neredeyse hep 00:xx:xx
            return h == 0 and m < 60 and sec < 60
        if len(parts) == 2:
            m, sec = parts
            return m < 60 and sec < 60
        return False

    @classmethod
    def _talk_from_timestamps(cls, row: dict[str, Any]) -> int:
        """Yalnızca answered/bridge → ended (start→end kullanma)."""
        answer_keys = (
            "answeredAt",
            "answered_at",
            "bridgeAt",
            "bridge_at",
            "connectTime",
            "connect_time",
        )
        end_keys = (
            "endedAt",
            "ended_at",
            "hangupAt",
            "hangup_at",
            "finishedAt",
            "finished_at",
            "completedAt",
            "completed_at",
        )
        # answerTime/endTime saat string'i olabilir — sadece ISO/datetime dene
        a_raw = cls._pick(row, answer_keys)
        e_raw = cls._pick(row, end_keys)
        if a_raw is None or e_raw is None:
            return 0
        a = cls._try_parse_single_datetime(a_raw)
        e = cls._try_parse_single_datetime(e_raw)
        if a is None or e is None:
            return 0
        delta = int((e[0] - a[0]).total_seconds())
        return delta if delta > 0 else 0

    @classmethod
    def _looks_like_duration_value(cls, raw: Any) -> bool:
        """00:01:26 / 1:26 / 86 — duvar saati 21:56:59 değil."""
        if isinstance(raw, bool):
            return False
        if isinstance(raw, (int, float)):
            n = float(raw)
            if 0 < n <= 6 * 3600:
                return True
            if 6 * 3600 < n <= 24 * 3600 * 1000:
                return True  # ms adayı
            return False
        if cls._is_panel_duration_string(raw):
            return True
        s = str(raw).strip().replace("：", ":")
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s.replace(".", ":")):
            parts = [int(p) for p in s.replace(".", ":").split(":")]
            if len(parts) == 3:
                return parts[0] == 0  # sadece 00:MM:SS
            return len(parts) == 2
        if re.fullmatch(r"\d+", s):
            return cls._looks_like_duration_value(int(s))
        return False

    @staticmethod
    def _is_ring_like_key(folded_key: str) -> bool:
        # "recording" içinde "ring" geçer — yanlış pozitif olmasın
        if "record" in folded_key:
            return False
        return any(
            x in folded_key
            for x in (
                "ring",
                "caldir",
                "waiting",
                "hold",
                "queuewait",
                "ringing",
            )
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
