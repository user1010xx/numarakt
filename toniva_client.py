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

        matches: list[CallRecord] = []
        parsed = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            rec = self._parse_row(row)
            if rec is None:
                continue
            parsed += 1
            if phones_equal(rec.phone, target):
                matches.append(rec)

        if parsed == 0:
            sample = rows[0] if isinstance(rows[0], dict) else {}
            keys = list(sample.keys()) if isinstance(sample, dict) else []
            raise RuntimeError(
                "Toniva satırları geldi ama telefon alanı okunamadı. "
                f"Örnek alan adları: {keys[:20]}. "
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
        # (tarih+saat sort_key en büyük olan; eşitlikte listedeki son kayıt)
        latest = max(
            enumerate(matches),
            key=lambda pair: (pair[1].sort_key, pair[0]),
        )[1]
        logger.info(
            "En son arama seçildi: phone=%s agent=%s at=%s %s (eşleşme=%s)",
            latest.phone,
            latest.agent_name,
            latest.call_date,
            latest.call_time,
            len(matches),
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
            return [r for r in data if isinstance(r, dict)]
        if not isinstance(data, dict):
            return []

        for key in ("rows", "data", "items", "results", "records", "conversations"):
            val = data.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
            if isinstance(val, dict):
                for inner in ("rows", "data", "items", "results"):
                    if isinstance(val.get(inner), list):
                        return [r for r in val[inner] if isinstance(r, dict)]

        # { "report": { "rows": [...] } }
        report = data.get("report")
        if isinstance(report, dict):
            return TonivaClient._extract_rows(report)

        return []

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

        Önemli: API bazen talkDuration/billsec=0 yazar ama asıl süre
        başka alanda (veya duration - ring) durur. İlk 0'da durma.
        """
        # 1) Spesifik talk alanları — ilk sıfırdan büyük değer
        for raw in cls._pick_all_present(row, _TALK_DURATION_KEYS):
            sec = cls._parse_duration_seconds(raw)
            if sec > 0:
                return sec

        # 2) Heuristik: gorusme / talk / billsec / answer / connected
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_ring_like_key(fk):
                continue
            if not any(
                x in fk
                for x in (
                    "gorusme",
                    "talk",
                    "billsec",
                    "bill",
                    "answer",
                    "connected",
                    "conversation",
                )
            ):
                continue
            sec = cls._parse_duration_seconds(val)
            if sec > 0:
                return sec

        ring = 0
        for raw in cls._pick_all_present(row, _RING_DURATION_KEYS):
            ring = max(ring, cls._parse_duration_seconds(raw))
        # Heuristik ring
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_ring_like_key(fk):
                ring = max(ring, cls._parse_duration_seconds(val))

        total = 0
        for raw in cls._pick_all_present(row, _TOTAL_DURATION_KEYS):
            total = max(total, cls._parse_duration_seconds(raw))
        # Heuristik: duration / sure (ring değil)
        for key, val in row.items():
            if val in (None, ""):
                continue
            fk = cls._fold_key(str(key))
            if cls._is_ring_like_key(fk):
                continue
            if "duration" in fk or "sure" in fk or fk.endswith("sec"):
                if any(x in fk for x in ("wait", "hold", "queue")):
                    continue
                total = max(total, cls._parse_duration_seconds(val))

        # 3) FreePBX tarzı: duration=toplam, görüşme ≈ total - ring
        if total > 0 and ring > 0:
            if total > ring:
                return total - ring
            # total == ring → cevaplanmamış / görüşme yok
            return 0
        if total > 0:
            return total

        return 0

    @staticmethod
    def _is_ring_like_key(folded_key: str) -> bool:
        return any(
            x in folded_key
            for x in ("ring", "caldir", "waiting", "hold", "queuewait")
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
        """Görüşme süresini saniyeye çevir. Ring/çaldırma alanları buraya gelmemeli."""
        if raw is None or raw == "" or raw in ("-", "—", "–"):
            return 0
        if isinstance(raw, bool):
            return 0
        if isinstance(raw, (int, float)):
            return max(0, int(raw))

        s = str(raw).strip()
        if not s:
            return 0

        # HH:MM:SS veya MM:SS
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
            parts = [int(p) for p in s.split(":")]
            if len(parts) == 3:
                h, m, sec = parts
                return max(0, h * 3600 + m * 60 + sec)
            if len(parts) == 2:
                m, sec = parts
                return max(0, m * 60 + sec)

        # "11 sn", "11s", "11 saniye"
        m = re.search(r"(\d+)", s)
        if m and re.search(r"sn|sec|saniye", s, re.I):
            return max(0, int(m.group(1)))

        # düz sayı string
        if re.fullmatch(r"\d+", s):
            return max(0, int(s))

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
