"""
Compute SLA due dates using BUSINESS HOURS (8:00-17:00, Mon-Fri).
If the start time falls outside business hours (late night, weekend),
the SLA clock starts counting from the beginning of the next business window.
"""
from datetime import datetime, timedelta, time

BUSINESS_START = time(8, 0)
BUSINESS_END = time(17, 0)
WORKDAYS = {0, 1, 2, 3, 4}  # Monday=0 ... Friday=4


def _snap_to_business_start(dt: datetime) -> datetime:
    """If dt falls outside business hours, snap it forward to the nearest business window."""
    while True:
        if dt.weekday() not in WORKDAYS:
            next_day = dt.date() + timedelta(days=1)
            dt = datetime.combine(next_day, BUSINESS_START, tzinfo=dt.tzinfo)
            continue
        if dt.time() < BUSINESS_START:
            dt = datetime.combine(dt.date(), BUSINESS_START, tzinfo=dt.tzinfo)
        elif dt.time() >= BUSINESS_END:
            next_day = dt.date() + timedelta(days=1)
            dt = datetime.combine(next_day, BUSINESS_START, tzinfo=dt.tzinfo)
            continue
        return dt


def add_business_hours(start: datetime, hours: float) -> datetime:
    """Add a number of 'working hours' (counted only within business hours) to start."""
    remaining = timedelta(hours=hours)
    current = _snap_to_business_start(start)

    while remaining > timedelta(0):
        day_end = datetime.combine(current.date(), BUSINESS_END, tzinfo=current.tzinfo)
        available_today = day_end - current

        if remaining <= available_today:
            current = current + remaining
            remaining = timedelta(0)
        else:
            remaining -= available_today
            next_day = current.date() + timedelta(days=1)
            current = _snap_to_business_start(
                datetime.combine(next_day, BUSINESS_START, tzinfo=current.tzinfo)
            )

    return current
