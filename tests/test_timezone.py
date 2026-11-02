import zoneinfo
from datetime import datetime, timedelta

import pytest

from app.config import settings_from_mapping


@pytest.mark.parametrize("name,hours", [("Europe/Moscow", 3), ("Asia/Yekaterinburg", 5)])
def test_timezone_works_without_system_zone_database(name, hours):
    previous = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(())
    zoneinfo.ZoneInfo.clear_cache()
    try:
        # Force the packaged tzdata path even if the default zone was preloaded
        # when app.config was imported on a host with system zone files.
        packaged = zoneinfo.ZoneInfo.no_cache(name)
        assert datetime(2026, 10, 19, 12, tzinfo=packaged).utcoffset() == timedelta(hours=hours)
        settings = settings_from_mapping({"TIMEZONE": name}, require_token=False)
        local = datetime(2026, 10, 19, 12, tzinfo=settings.timezone)
        assert local.utcoffset() == timedelta(hours=hours)
    finally:
        zoneinfo.reset_tzpath(previous)
        zoneinfo.ZoneInfo.clear_cache()
