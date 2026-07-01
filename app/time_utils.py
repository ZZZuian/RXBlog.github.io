"""UTC storage and China Standard Time presentation helpers."""

from datetime import timedelta, timezone


CHINA_TIMEZONE = timezone(timedelta(hours=8), name='Asia/Shanghai')


def to_china_time(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CHINA_TIMEZONE)


def format_china_time(value, pattern='%Y-%m-%d %H:%M'):
    localized = to_china_time(value)
    return localized.strftime(pattern) if localized else ''
