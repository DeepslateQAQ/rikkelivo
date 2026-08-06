"""Timestamp helpers shared by both backup formats.

Both apps store message timestamps as naive-local ISO 8601 strings with
millisecond precision (e.g. "2026-08-06T13:56:06.000").  RikkaHub additionally
stores conversation timestamps as Unix epoch milliseconds.

A naive ISO string carries no timezone, so the epoch-ms conversion assumes the
machine running the converter uses the same local timezone as the device that
created the backup.  This is documented in the README; round trips are stable
as long as both runs happen in one timezone.
"""

from __future__ import annotations

from datetime import datetime, timezone

_MS_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def iso_to_ms(value: str | None) -> int | None:
    """Parse a naive or Z-suffixed ISO timestamp into epoch milliseconds.

    Naive timestamps are interpreted in the converter's local timezone;
    Z-suffixed ones are converted to local time first.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int | None) -> str | None:
    """Format epoch milliseconds as a naive-local ISO string with 3-digit ms."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000).strftime(_MS_FMT)[:-3]


def iso_to_utc_z(value: str | None) -> str | None:
    """Normalize an ISO timestamp to UTC with a Z suffix (Instant format).

    RikkaHub reasoning parts use kotlinx-datetime Instant (requires an offset,
    conventionally Z); Kelivo reasoning timestamps are naive local times.
    Naive values are interpreted in the converter's local timezone.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        return text
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime(_MS_FMT)[:-3] + "Z"
