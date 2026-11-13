"""Timezone-aware 5-field cron parsing and schedule calculation (SF-022)."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Optional, Set
import zoneinfo


def parse_cron_field(field_str: str, min_val: int, max_val: int) -> Set[int]:
    """Parse a single cron field into a set of matching integers."""
    field_str = field_str.strip()
    if field_str == "*":
        return set(range(min_val, max_val + 1))

    results: Set[int] = set()
    parts = field_str.split(",")
    for part in parts:
        part = part.strip()
        # Step notation */5 or 1-10/2
        if "/" in part:
            subparts = part.split("/")
            step = int(subparts[1])
            if subparts[0] == "*":
                sub_range = range(min_val, max_val + 1)
            elif "-" in subparts[0]:
                r_parts = subparts[0].split("-")
                sub_range = range(int(r_parts[0]), int(r_parts[1]) + 1)
            else:
                sub_range = range(int(subparts[0]), max_val + 1)
            results.update([x for x in sub_range if (x - list(sub_range)[0]) % step == 0])
        elif "-" in part:
            r_parts = part.split("-")
            results.update(range(int(r_parts[0]), int(r_parts[1]) + 1))
        else:
            results.add(int(part))

    return {x for x in results if min_val <= x <= max_val}


def is_cron_match(
    cron_expr: str,
    dt: datetime,
    timezone_str: str = "Asia/Jakarta",
) -> bool:
    """Check if given datetime matches 5-field cron expression in specified timezone."""
    tz = zoneinfo.ZoneInfo(timezone_str)
    # Ensure dt is in target timezone
    dt_tz = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC")).astimezone(tz)

    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Format cron harus memiliki 5 field (ditemukan {len(fields)}): '{cron_expr}'")

    minute_field, hour_field, dom_field, month_field, dow_field = fields

    minutes = parse_cron_field(minute_field, 0, 59)
    hours = parse_cron_field(hour_field, 0, 23)
    doms = parse_cron_field(dom_field, 1, 31)
    months = parse_cron_field(month_field, 1, 12)

    # In Python weekday(): Monday is 0, Sunday is 6.
    # In standard cron: Sunday is 0 or 7, Monday is 1, ..., Saturday is 6.
    dow_cron = (dt_tz.weekday() + 1) % 7
    dows = parse_cron_field(dow_field, 0, 7)
    # Allow 7 as alias for 0 (Sunday)
    if 7 in dows:
        dows.add(0)

    if dt_tz.minute not in minutes:
        return False
    if dt_tz.hour not in hours:
        return False
    if dt_tz.day not in doms:
        return False
    if dt_tz.month not in months:
        return False
    if dow_cron not in dows:
        return False

    return True


def format_schedule_key(
    occurrence_dt: datetime,
    prefix: str = "auto_research",
    timezone_str: str = "Asia/Jakarta",
) -> str:
    """Compute deterministic and unique schedule key for an occurrence."""
    tz = zoneinfo.ZoneInfo(timezone_str)
    dt_tz = occurrence_dt.astimezone(tz) if occurrence_dt.tzinfo else occurrence_dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC")).astimezone(tz)
    # Minute-level canonical key
    timestamp_str = dt_tz.strftime("%Y%m%d_%H%M")
    return f"{prefix}_{timestamp_str}"

