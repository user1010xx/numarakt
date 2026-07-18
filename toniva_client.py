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
_DATE_KEYS = (
    "tarih",
    "TARİH",
    "Tarih",
    "date",
    "callDate",
    "call_date",
    "startedAt",
    "started_at",
    "startTime",
    "start_time",
    "createdAt",
    "created_at",
    "datetime",
    "timestamp",
)
_TIME_KEYS = (
    "saat",
    "SAAT",
    "Saat",
    "time",
    "callTime",
    "call_time",
    "hour",
)
# Yalnızca görüşme süresi — çaldırma / ring alanları KASITLI olarak yok
_TALK_DURATION_KEYS = (
    "gorusmeSuresi",
    "görüşmeSüresi",
    "gorusme_suresi",
    "GÖRÜŞME SÜRESİ",
    "Görüşme Süresi",
    "talkDuration",
    "talk_duration",
    "billsec",
    "billSec",
    "answeredDuration",
    "answered_duration",
    "conversationDuration",
    "conversation_duration",
    "talkTime",
    "talk_time",
    "connectedDuration",
    "connected_duration",
    # Genel isimler en sonda (ring/total ile karışma riski)
    "callDuration",
    "call_duration",
    "duration",
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

        matches.sort(key=lambda r: r.sort_key, reverse=True)
        return matches[0]

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
        agent_str = str(agent).strip() if agent is not None else "—"
        if not agent_str:
            agent_str = "—"

        date_raw = self._pick(row, _DATE_KEYS)
        time_raw = self._pick(row, _TIME_KEYS)
        talk_raw = self._pick(row, _TALK_DURATION_KEYS)

        sort_dt, date_disp, time_disp = self._resolve_datetime(date_raw, time_raw)
        talk_seconds = self._parse_duration_seconds(talk_raw)

        display_phone = normalize_tr_phone(phone_str) or digits_keep(phone_str)

        return CallRecord(
            agent_name=agent_str,
            phone=display_phone,
            call_date=date_disp,
            call_time=time_disp,
            talk_seconds=talk_seconds,
            sort_key=sort_dt,
        )

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
    s = s.strip()
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
