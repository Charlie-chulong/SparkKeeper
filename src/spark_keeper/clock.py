from __future__ import annotations

from datetime import datetime, timedelta, timezone

# China has used UTC+08:00 without daylight saving time since 1991. A fixed
# offset also works on Windows installations without the optional tzdata package.
BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_date_iso() -> str:
    return now_beijing().date().isoformat()


def display_beijing_time(value: str) -> str:
    """Render aware stored instants in Beijing time; never guess old naive zones."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        return f"{value}（原记录未标注时区）"
    return moment.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
