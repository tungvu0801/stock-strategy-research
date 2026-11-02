"""research_data/nyse_calendar.py — the NYSE full-day closures of 2016-2026, as a static, dated table (no calendar library, no network).

Every weekday that is not listed here is a regular session. The table is cross-checked by the tests against the 60 closures of the Stage 6.4
IEX bundle (2020-07-27 -> 2026-10-23, recorded in STAGE_7_8C_REAL_DATA_READINESS_AUDIT.md) and against autorun/calendar.py for 2026. Early
closes (13:00) are listed for information only: daily bars cannot show them, and no check reads them."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List

COVERED_YEARS = (2016, 2026)
CLOSURES: Dict[str, str] = {
    "2016-01-01": "New Year's Day", "2016-01-18": "Martin Luther King Jr. Day", "2016-02-15": "Washington's Birthday", "2016-03-25": "Good Friday",
    "2016-05-30": "Memorial Day", "2016-07-04": "Independence Day", "2016-09-05": "Labor Day", "2016-11-24": "Thanksgiving Day",
    "2016-12-26": "Christmas Day (observed)",
    "2017-01-02": "New Year's Day (observed)", "2017-01-16": "Martin Luther King Jr. Day", "2017-02-20": "Washington's Birthday",
    "2017-04-14": "Good Friday", "2017-05-29": "Memorial Day", "2017-07-04": "Independence Day", "2017-09-04": "Labor Day",
    "2017-11-23": "Thanksgiving Day", "2017-12-25": "Christmas Day",
    "2018-01-01": "New Year's Day", "2018-01-15": "Martin Luther King Jr. Day", "2018-02-19": "Washington's Birthday", "2018-03-30": "Good Friday",
    "2018-05-28": "Memorial Day", "2018-07-04": "Independence Day", "2018-09-03": "Labor Day", "2018-11-22": "Thanksgiving Day",
    "2018-12-05": "National Day of Mourning (President George H. W. Bush)", "2018-12-25": "Christmas Day",
    "2019-01-01": "New Year's Day", "2019-01-21": "Martin Luther King Jr. Day", "2019-02-18": "Washington's Birthday", "2019-04-19": "Good Friday",
    "2019-05-27": "Memorial Day", "2019-07-04": "Independence Day", "2019-09-02": "Labor Day", "2019-11-28": "Thanksgiving Day",
    "2019-12-25": "Christmas Day",
    "2020-01-01": "New Year's Day", "2020-01-20": "Martin Luther King Jr. Day", "2020-02-17": "Washington's Birthday", "2020-04-10": "Good Friday",
    "2020-05-25": "Memorial Day", "2020-07-03": "Independence Day (observed)", "2020-09-07": "Labor Day", "2020-11-26": "Thanksgiving Day",
    "2020-12-25": "Christmas Day",
    "2021-01-01": "New Year's Day", "2021-01-18": "Martin Luther King Jr. Day", "2021-02-15": "Washington's Birthday", "2021-04-02": "Good Friday",
    "2021-05-31": "Memorial Day", "2021-07-05": "Independence Day (observed)", "2021-09-06": "Labor Day", "2021-11-25": "Thanksgiving Day",
    "2021-12-24": "Christmas Day (observed)",
    "2022-01-17": "Martin Luther King Jr. Day", "2022-02-21": "Washington's Birthday", "2022-04-15": "Good Friday", "2022-05-30": "Memorial Day",
    "2022-06-20": "Juneteenth (observed)", "2022-07-04": "Independence Day", "2022-09-05": "Labor Day", "2022-11-24": "Thanksgiving Day",
    "2022-12-26": "Christmas Day (observed)",
    "2023-01-02": "New Year's Day (observed)", "2023-01-16": "Martin Luther King Jr. Day", "2023-02-20": "Washington's Birthday",
    "2023-04-07": "Good Friday", "2023-05-29": "Memorial Day", "2023-06-19": "Juneteenth", "2023-07-04": "Independence Day", "2023-09-04": "Labor Day",
    "2023-11-23": "Thanksgiving Day", "2023-12-25": "Christmas Day",
    "2024-01-01": "New Year's Day", "2024-01-15": "Martin Luther King Jr. Day", "2024-02-19": "Washington's Birthday", "2024-03-29": "Good Friday",
    "2024-05-27": "Memorial Day", "2024-06-19": "Juneteenth", "2024-07-04": "Independence Day", "2024-09-02": "Labor Day",
    "2024-11-28": "Thanksgiving Day", "2024-12-25": "Christmas Day",
    "2025-01-01": "New Year's Day", "2025-01-09": "National Day of Mourning (President Jimmy Carter)", "2025-01-20": "Martin Luther King Jr. Day",
    "2025-02-17": "Washington's Birthday", "2025-04-18": "Good Friday", "2025-05-26": "Memorial Day", "2025-06-19": "Juneteenth",
    "2025-07-04": "Independence Day", "2025-09-01": "Labor Day", "2025-11-27": "Thanksgiving Day", "2025-12-25": "Christmas Day",
    "2026-01-01": "New Year's Day", "2026-01-19": "Martin Luther King Jr. Day", "2026-02-16": "Washington's Birthday", "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day", "2026-06-19": "Juneteenth", "2026-07-03": "Independence Day (observed)", "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day", "2026-12-25": "Christmas Day",
}
EARLY_CLOSES = ("2016-11-25", "2017-07-03", "2017-11-24", "2018-07-03", "2018-11-23", "2018-12-24", "2019-07-03", "2019-11-29", "2019-12-24",
                "2020-11-27", "2020-12-24", "2021-11-26", "2022-11-25", "2023-07-03", "2023-11-24", "2024-07-03", "2024-11-29", "2024-12-24",
                "2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24")   # information only (not visible in daily bars)
SOURCE = "NYSE holiday schedule as published (full-day closures incl. the 2018-12-05 and 2025-01-09 national days of mourning); static table"


class CalendarError(ValueError):
    pass


def _check_range(start: str, end: str) -> None:
    lo, hi = COVERED_YEARS
    if not (lo <= int(start[:4]) <= hi and lo <= int(end[:4]) <= hi) or start > end:
        raise CalendarError(f"the calendar covers {lo}-{hi} only; asked {start} -> {end}")


def sessions(start: str, end: str) -> List[str]:
    """Every NYSE session in [start, end] (weekdays that are not a full-day closure)."""
    _check_range(start, end)
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        s = d.isoformat()
        if d.weekday() < 5 and s not in CLOSURES:
            out.append(s)
        d += timedelta(days=1)
    return out


def compare(dates) -> dict:
    """A dataset calendar against the table over its own span: extra dates (a weekend or a closure), missing sessions."""
    ds = sorted(set(dates))
    if not ds:
        return {"status": "EMPTY", "extra": [], "missing": [], "weekend": [], "closures": []}
    try:
        expect = sessions(ds[0], ds[-1])
    except CalendarError as exc:
        return {"status": "NOT_COVERED", "detail": str(exc), "extra": [], "missing": [], "weekend": [], "closures": []}
    have = set(ds)
    weekend = [d for d in ds if date.fromisoformat(d).weekday() >= 5]
    closures = [d for d in ds if d in CLOSURES]
    extra = sorted(have - set(expect))
    missing = sorted(set(expect) - have)
    return {"status": "MATCH" if not (extra or missing) else "MISMATCH", "start": ds[0], "end": ds[-1], "sessions": len(ds), "expected": len(expect),
            "extra": extra, "missing": missing, "weekend": weekend, "closures": closures}
