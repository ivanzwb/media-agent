from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Explicit format strings tried before falling back to dateparser / regex.
_EXPLICIT_FORMATS: list[str] = [
    # ISO 8601 variants
    "%Y-%m-%dT%H:%M:%S%z",       # 2026-06-28T10:30:00+0800
    "%Y-%m-%dT%H:%M:%S.%f%z",    # 2026-06-28T10:30:00.123+0800
    "%Y-%m-%dT%H:%M:%S",         # 2026-06-28T10:30:00 (no tz)
    "%Y-%m-%dT%H:%M:%S Z",       # 2026-06-28T10:30:00 Z
    "%Y-%m-%dT%H:%M:%S.%f",      # 2026-06-28T10:30:00.123456
    "%Y-%m-%d",                   # 2026-06-28
    # RFC 2822 / email style
    "%a, %d %b %Y %H:%M:%S %z",  # Sat, 28 Jun 2026 10:30:00 +0800
    "%d %b %Y %H:%M:%S %z",      # 28 Jun 2026 10:30:00 +0800
    "%a, %d %b %Y %H:%M:%S",     # Sat, 28 Jun 2026 10:30:00
    "%d %b %Y %H:%M:%S",         # 28 Jun 2026 10:30:00
    # Common web formats
    "%b %d, %Y",                  # Jun 28, 2026
    "%d %B %Y",                   # 28 June 2026
    "%B %d, %Y",                  # June 28, 2026
    "%Y/%m/%d",                   # 2026/06/28
    # Chinese formats
    "%Y年%m月%d日",               # 2026年6月28日
    "%Y-%m-%d %H:%M:%S",         # 2026-06-28 10:30:00
    "%Y-%m-%d %H:%M",            # 2026-06-28 10:30
    # US style
    "%m/%d/%Y",                   # 06/28/2026
]

# Regex to extract a 4-digit year as absolute last resort.
_YEAR_RE = re.compile(r"\b(19\d{2}|20[0-2]\d)\b")


def parse_date(text: str | None) -> datetime | None:
    """Parse a date string into a timezone-aware UTC datetime.

    Fallback chain (first match wins):
      1. ``dateparser`` (handles human-readable, relative, multi-lang)
      2. Explicit format list (ISO 8601, RFC 2822, Chinese, US …)
      3. Regex year extraction (just year-01-01, to avoid total data loss)

    Returns ``None`` when all strategies fail (logged at DEBUG level).
    """
    if not text or not text.strip():
        return None
    text = text.strip()

    # --- 1. dateparser (rich NLP parsing) ---
    dt = _try_dateparser(text)
    if dt is not None:
        return _utc(dt)

    # --- 2. Explicit format list ---
    dt = _try_explicit(text)
    if dt is not None:
        return _utc(dt)

    # --- 3. Fallback: extract year only ---
    m = _YEAR_RE.search(text)
    if m:
        year = int(m.group(1))
        logger.debug(
            "fallback year-only for %r → %d", text, year,
        )
        return datetime(year, 1, 1, tzinfo=timezone.utc)

    logger.debug("unable to parse date: %r", text)
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    """Ensure the datetime is timezone-aware and converted to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _try_dateparser(text: str) -> datetime | None:
    try:
        import dateparser
    except ImportError:
        return None
    try:
        return dateparser.parse(
            text,
            settings={
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": "UTC",
                "PREFER_DATES_FROM": "past",
            },
        )
    except Exception:
        logger.debug("dateparser failed on %r", text, exc_info=True)
        return None


def _try_explicit(text: str) -> datetime | None:
    """Try the explicit format list.

    Heuristic: if the string contains Chinese characters, skip formats that
    would misinterpret them (e.g. English month names) and try the Chinese
    format first.
    """
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))

    formats = list(_EXPLICIT_FORMATS)

    if has_cjk:
        # Push CJK-friendly formats to the front.
        cjk_first = [f for f in formats if "年" in f]
        others = [f for f in formats if "年" not in f]
        formats = cjk_first + others

    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            return dt
        except ValueError:
            continue
    return None
