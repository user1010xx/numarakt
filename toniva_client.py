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
        Görüşme süresi (sn). Çaldırma ile karıştırılmaz.

        Canlı hata: personel/tarih/saat geliyor, talk=0.
        → Süre alanı ya farklı isimde, ya nested, ya da 00:MM:SS taraması şart.
        """
        # 1) Spesifik talk alanları — sıfırları atla
        for raw in cls._pick_all_present(row, _TALK_DURATION_KEYS):
            sec = cls._parse_duration_seconds(raw)
            if sec > 0:
                return sec

        # 2) Heuristik talk-benzeri anahtarlar
        talk_hint = (
            "gorusme",
            "talk",
            "billsec",
            "bill",
            "answer",
            "connected",
            "conversation",
            "bridge",
            "speaking",
            "handle",
            "service",
            "incall",
            "active",
        )
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_ring_like_key(fk):
                continue
            if not any(x in fk for x in talk_hint):
                continue
            # answeredAt gibi timestamp'i süre sanma
            if any(x in fk for x in ("at", "date", "time", "stamp")) and not any(
                x in fk for x in ("duration", "sec", "sure", "billsec", "talk")
            ):
                if not cls._looks_like_duration_value(val):
                    continue
            sec = cls._parse_duration_seconds(raw=val)
            if sec > 0:
                return sec

        ring = cls._max_duration_for_keys(row, _RING_DURATION_KEYS, ring_only=True)
        total = cls._max_duration_for_keys(row, _TOTAL_DURATION_KEYS, ring_only=False)

        # Heuristik duration/sure
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_ring_like_key(fk):
                ring = max(ring, cls._parse_duration_seconds(val))
                continue
            if "duration" in fk or "sure" in fk or fk.endswith("sec") or fk.endswith("secs"):
                if any(x in fk for x in ("wait", "hold", "queue")):
                    continue
                total = max(total, cls._parse_duration_seconds(val))

        # 3) total - ring
        if total > 0 and ring > 0:
            if total > ring:
                return total - ring
            return 0
        if total > 0:
            return total

        # 4) Timestamp farkı: ended - answered / hangup - answer
        from_ts = cls._talk_from_timestamps(row)
        if from_ts > 0:
            return from_ts

        # 5) Tüm satırda 00:MM:SS / MM:SS formatlı süre değerlerini tara
        #    (saat 21:56:59 gibi saat değerlerini ele — saat kısmı >= 3 ise clock say)
        scanned = cls._scan_duration_values(row)
        if scanned > 0:
            return scanned

        return 0

    @classmethod
    def _max_duration_for_keys(
        cls,
        row: dict[str, Any],
        keys: tuple[str, ...],
        *,
        ring_only: bool,
    ) -> int:
        best = 0
        for raw in cls._pick_all_present(row, keys):
            best = max(best, cls._parse_duration_seconds(raw))
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if ring_only:
                if cls._is_ring_like_key(fk):
                    best = max(best, cls._parse_duration_seconds(val))
            else:
                if cls._is_ring_like_key(fk):
                    continue
        return best

    @classmethod
    def _talk_from_timestamps(cls, row: dict[str, Any]) -> int:
        """answeredAt/endedAt farkından görüşme süresi."""
        answer_keys = (
            "answeredAt",
            "answered_at",
            "answerTime",
            "answer_time",
            "bridgeAt",
            "bridge_at",
            "connectTime",
            "connect_time",
        )
        end_keys = (
            "endedAt",
            "ended_at",
            "endTime",
            "end_time",
            "hangupAt",
            "hangup_at",
            "finishedAt",
            "finished_at",
            "completedAt",
            "completed_at",
        )
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
    def _scan_duration_values(cls, row: dict[str, Any]) -> int:
        """
        Anahtar adı bilinmese bile süre formatındaki değerleri topla.
        Ring anahtarlarını ve duvar saati (21:56:59) değerlerini ele.
        """
        talk_like = 0
        ring_like = 0
        neutral = 0
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            # net telefon / id / isim alanları
            if any(
                x in fk
                for x in (
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
                )
            ):
                continue
            if not cls._looks_like_duration_value(val):
                continue
            sec = cls._parse_duration_seconds(val)
            if sec <= 0:
                continue
            if cls._is_ring_like_key(fk):
                ring_like = max(ring_like, sec)
            elif any(
                x in fk
                for x in (
                    "gorusme",
                    "talk",
                    "bill",
                    "answer",
                    "bridge",
                    "connected",
                    "conversation",
                    "duration",
                    "sure",
                )
            ):
                talk_like = max(talk_like, sec)
            else:
                # Anahtar belirsiz ama değer 00:01:26 gibi — aday
                neutral = max(neutral, sec)

        if talk_like > 0:
            return talk_like
        if neutral > 0 and ring_like > 0 and neutral > ring_like:
            return neutral - ring_like if neutral != ring_like else 0
        if neutral > 0 and ring_like == 0:
            # Tek süre değeri; saat değilse (looks_like_duration) görüşme kabul
            return neutral
        return 0

    @classmethod
    def _looks_like_duration_value(cls, raw: Any) -> bool:
        """00:01:26 / 1:26 / 86 — duvar saati 21:56:59 değil."""
        if isinstance(raw, bool):
            return False
        if isinstance(raw, (int, float)):
            n = float(raw)
            # 0..6 saat makul görüşme; çok büyük sayılar ms olabilir
            if 0 < n <= 6 * 3600:
                return True
            if 6 * 3600 < n <= 24 * 3600 * 1000:
                return True  # ms adayı
            return False
        s = str(raw).strip().replace("：", ":").replace(".", ":")
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
            parts = [int(p) for p in s.split(":")]
            if len(parts) == 3:
                h, m, sec = parts
                # Duvar saati genelde h>=1 ve toplam büyük; süre formatı panelde 00:01:26
                if h == 0:
                    return True
                if h < 3 and m < 60:
                    # 01:26:00 gibi uzun görüşme mümkün; 21:56:59 clock
                    # Saat 3+ ise clock kabul (çağrı paneli saati)
                    return h < 3
                return False
            if len(parts) == 2:
                return True
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
