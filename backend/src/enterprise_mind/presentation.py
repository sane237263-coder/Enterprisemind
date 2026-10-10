"""User-facing time formatting; storage and API instants keep their original timezone."""

from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


def format_beijing_time(value: datetime | str | None) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return "—"
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return "—"
    return value.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S（北京时间）")
