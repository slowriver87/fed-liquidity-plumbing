"""Next-events calendar (CAL) from fixed rules and the parsed source calendars.

- H.4.1: every Thursday at 4:30 pm ET, or the next business day when Thursday is a federal holiday (the Fed
  posts no machine-readable list of release dates).
- Month-end / quarter-end: the last business day of each month (federal holidays excluded).
- FOMC decision (2:00 pm on day two), BEA personal income and outlays, the end of the current purchase period.
"""
from __future__ import annotations

import calendar as _cal
import datetime as dt

from .common import day_label, is_business_day, time_label

WINDOW_DAYS = 28
EV_RMP = "Current purchase period ends; next monthly purchase plan due (release date not posted)"
MONTH_FULL = list(_cal.month_name)


def h41_releases(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """[(release date, Wednesday covered)] with release dates in [start, end]."""
    out = []
    w = start - dt.timedelta(days=(start.weekday() - 2) % 7 + 7)
    while w <= end:
        r = w + dt.timedelta(1)
        while not is_business_day(r):
            r += dt.timedelta(1)
        if start <= r <= end:
            out.append((r, w))
        w += dt.timedelta(7)
    return out


def last_business_day(y: int, m: int) -> dt.date:
    d = dt.date(y, m, _cal.monthrange(y, m)[1])
    while not is_business_day(d):
        d -= dt.timedelta(1)
    return d


def month_ends(start: dt.date, end: dt.date) -> list[dict]:
    out, y, m = [], start.year, start.month
    while dt.date(y, m, 1) <= end:
        d = last_business_day(y, m)
        if start <= d <= end:
            if m in (3, 6, 9, 12):
                out.append({"d": d.isoformat(), "t": "all day", "id": "qe", "src": "Calendar",
                            "ev": f"Last business day of {MONTH_FULL[m]} and of the quarter; quarter-ends can push repo rates up for a day"})
            else:
                out.append({"d": d.isoformat(), "t": "all day", "id": "me", "src": "Calendar",
                            "ev": f"Last business day of {MONTH_FULL[m]}; month-ends can push repo rates up for a day"})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def sort_key(c: dict):
    t = c["t"]
    if t in ("all day", "by this date"):
        return c["d"], -1
    hm, ap = t.split(" ")
    h, mi = map(int, hm.split(":"))
    return c["d"], (h % 12 + (12 if ap == "pm" else 0)) * 60 + mi


def build(today: dt.date, last_release: str | None, fomc: list[list[str]] | None, pce: list[dict] | None,
          rmp_to: str | None, old: list[dict]) -> list[dict]:
    """Events from today through WINDOW_DAYS. A source calendar passed as None was not reached: its rows
    are kept from the old CAL (those still ahead)."""
    end = today + dt.timedelta(WINDOW_DAYS)
    t0, t1 = today.isoformat(), end.isoformat()
    keep = lambda ids: [c for c in old if c["id"] in ids and c["d"] >= t0]
    out = []
    for r, w in h41_releases(today, end):
        if last_release and r.isoformat() <= last_release:
            continue
        out.append({"d": r.isoformat(), "t": "4:30 pm", "ev": f"H.4.1 balance sheet, week of {day_label(w.isoformat())}",
                    "src": "Federal Reserve", "id": "h41"})
    if fomc is None:
        out += keep({"fomc"})
    else:
        out += [{"d": m[1], "t": "2:00 pm", "ev": "FOMC decision", "src": "Federal Reserve", "id": "fomc"}
                for m in fomc if t0 <= m[1] <= t1]
    if pce is None:
        out += keep({"pce"})
    else:
        out += [{"d": p["d"], "t": time_label(*p["hm"]),
                 "ev": f"Personal income and outlays, {MONTH_FULL[int(p['ref'][5:])]} (core PCE)", "src": "BEA", "id": "pce"}
                for p in pce if t0 <= p["d"] <= t1]
    out += month_ends(today, end)
    if rmp_to and t0 <= rmp_to <= t1:
        out.append({"d": rmp_to, "t": "by this date", "ev": EV_RMP, "src": "New York Fed", "id": "rmp"})
    out.sort(key=sort_key)
    return [{"d": c["d"], "t": c["t"], "ev": c["ev"], "src": c["src"], "id": c["id"]} for c in out]
