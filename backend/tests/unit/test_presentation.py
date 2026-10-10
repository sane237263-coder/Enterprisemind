from datetime import UTC, datetime

from enterprise_mind.presentation import format_beijing_time


def test_beijing_time_formats_instants_and_preserves_the_input() -> None:
    original = datetime(2026, 10, 4, 11, 39, 49, 723888, tzinfo=UTC)
    assert format_beijing_time(original) == "2026-10-04 19:39:49（北京时间）"
    assert original.hour == 11 and original.tzinfo is UTC
    assert format_beijing_time(original.isoformat()) == format_beijing_time(original)
    assert format_beijing_time("2026-10-04T19:39:49+08:00") == format_beijing_time(original)
    assert format_beijing_time("2026-10-04T04:39:49-07:00") == format_beijing_time(original)
    assert format_beijing_time("2026-12-31T16:00:00Z") == "2027-01-01 00:00:00（北京时间）"


def test_unknown_timezone_and_invalid_values_have_a_safe_placeholder() -> None:
    for value in (None, "", "invalid", "2026-10-04", "2026-10-04T11:39:49", datetime(2026, 10, 4)):
        assert format_beijing_time(value) == "—"
