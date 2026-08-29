"""Date/time helpers for UTC storage and company-local business dates."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"
UTC = timezone.utc


def parse_date_yyyy_mm_dd(value):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def utcnow_naive():
    """Return UTC now without tzinfo for compatibility with current DB columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def resolve_timezone(tz_name=None):
    """Resolve an IANA timezone safely, falling back to Argentina."""
    name = (str(tz_name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def utc_naive_to_aware(value):
    """Interpret a DB-naive timestamp as UTC without changing its instant."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=UTC)


def utc_naive_to_local(value, tz_name=None):
    """Convert a DB-naive UTC timestamp to the requested company timezone."""
    aware = utc_naive_to_aware(value)
    if aware is None:
        return None
    return aware.astimezone(resolve_timezone(tz_name))


def local_now(tz_name=None):
    """Return the current aware datetime in the requested company timezone."""
    return datetime.now(UTC).astimezone(resolve_timezone(tz_name))


def local_today(tz_name=None):
    """Return today's calendar date according to the company timezone."""
    return local_now(tz_name).date()


def local_day_bounds_utc_naive(day=None, tz_name=None):
    """
    Return [start, end) for one company-local calendar day, represented as
    UTC-naive datetimes suitable for the existing database columns.

    Using half-open ranges avoids DST edge cases and prevents the application
    from switching the business date at 21:00 Argentina time merely because
    the database stores UTC.
    """
    tz = resolve_timezone(tz_name)
    target_day = day or local_today(tz_name)
    start_local = datetime.combine(target_day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(UTC).replace(tzinfo=None)
    end_utc = end_local.astimezone(UTC).replace(tzinfo=None)
    return start_utc, end_utc


def local_month_start_utc_naive(tz_name=None):
    """Return the UTC-naive instant corresponding to the local first day."""
    today = local_today(tz_name)
    start_local = datetime(today.year, today.month, 1, tzinfo=resolve_timezone(tz_name))
    return start_local.astimezone(UTC).replace(tzinfo=None)


def format_local_datetime(value, tz_name=None, fmt="%Y-%m-%d %H:%M"):
    """Format a stored UTC-naive timestamp in company-local time."""
    local_value = utc_naive_to_local(value, tz_name)
    return local_value.strftime(fmt) if local_value else ""


def format_local_date(value, tz_name=None, fmt="%Y-%m-%d"):
    """Format a stored UTC-naive timestamp as a company-local calendar date."""
    local_value = utc_naive_to_local(value, tz_name)
    return local_value.strftime(fmt) if local_value else ""
