"""Human-facing time formatting for vault notes.

Every league in this system plays on Eastern time (ticker grammar embeds ET
start times too — see league_matching.py); notes meant for a human to read
(daily slates, active-game state) show times in ET, 12-hour clock, not UTC
military time. Machine-consumed timestamps (signal log JSON, frontmatter
`updated`/`fetched_at` fields) stay ISO-8601 UTC — only display strings use
this.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def fmt_et(dt: datetime) -> str:
    """2026-07-17T23:05:00+00:00 -> '7/17 7:05 PM ET' (12-hour, no leading zero)."""
    local = dt.astimezone(ET)
    hour12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local.month}/{local.day} {hour12}:{local.minute:02d} {ampm} ET"
