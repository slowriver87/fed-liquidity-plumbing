"""Federal Reserve FOMC meeting calendar (layout printed Oct 20, 2026: `.panel` with heading
"2026 FOMC Meetings", rows `.fomc-meeting__month` "October" / "Apr/May" and `.fomc-meeting__date` "27-28" /
"17-18*"). Adapted from the Yield Regime Monitor's sources/fomc.py to keep both meeting days; the decision
is at 2:00 pm on day two."""
from __future__ import annotations

import datetime as dt
import re

from .common import MONTH_NAME, SourceError, cell_text, fetch

URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"


def _month(name: str) -> int:
    name = name.strip()
    m = next((v for k, v in MONTH_NAME.items() if k == name or k[:3] == name[:3]), None)
    if m is None:
        raise SourceError(f"FOMC calendar: unexpected month {name!r}")
    return m


def parse(html: bytes | str, years: list[int]) -> list[list[str]]:
    """-> [[day1, day2], ...] (ISO) for scheduled two-day meetings in `years`, ascending."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for y in years:
        panels = [p for p in soup.select(".panel")
                  if p.find(class_="panel-heading") and cell_text(p.find(class_="panel-heading")) == f"{y} FOMC Meetings"]
        if len(panels) != 1:
            continue  # next year's panel may not be posted yet
        for row in panels[0].select(".fomc-meeting"):
            mo, da = row.select_one(".fomc-meeting__month"), row.select_one(".fomc-meeting__date")
            if not mo or not da:
                continue
            m = re.fullmatch(r"(\d{1,2})-(\d{1,2})\*?", cell_text(da))
            if not m:
                continue  # notation votes, unscheduled or one-day meetings
            months = [_month(x) for x in cell_text(mo).split("/")]  # a meeting can span two months: 'Apr/May'
            d1 = dt.date(y, months[0], int(m.group(1)))
            d2 = dt.date(y, months[-1], int(m.group(2)))
            if not 0 < (d2 - d1).days <= 2:
                raise SourceError(f"FOMC calendar: unexpected meeting {cell_text(mo)} {cell_text(da)} {y}")
            out.append([d1.isoformat(), d2.isoformat()])
    if not out:
        raise SourceError("FOMC calendar: no meetings found")
    return sorted(out)


def get(years: list[int]) -> list[list[str]]:
    return parse(fetch(URL), years)
