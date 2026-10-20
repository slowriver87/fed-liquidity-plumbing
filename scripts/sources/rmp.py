"""New York Fed Treasury purchase schedule (operation period table; no data file).

Layout printed Oct 20, 2026: one table with header "Period | Planned Operation Amount | Tentative Schedule |
Results | Next Schedule Release", newest period first, periods written "10/15/2026 - 11/13/2026" (no zero
padding). Planned-amount sentences in 2026 take two forms, both matched strictly; anything else raises,
since a wording change is likely exactly when policy changes.
"""
from __future__ import annotations

import re

from .common import SourceError, cell_text, fetch, mdy

URL = ("https://www.newyorkfed.org/markets/domestic-market-operations/monetary-policy-implementation/"
       "treasury-securities/treasury-securities-operational-details")
HEADER = ["Period", "Planned Operation Amount", "Tentative Schedule", "Results", "Next Schedule Release"]
PERIOD = r"(\d{1,2}/\d{1,2}/\d{4}) - (\d{1,2}/\d{1,2}/\d{4})"
AMT = r"\$(\d+(?:\.\d+)?) billion"
FORMS = (
    (re.compile(r"The Desk plans to conduct approximately " + AMT + r" in reinvestment purchases and no reserve "
                r"management purchases over the noted monthly period\."), False),
    (re.compile(r"The Desk plans to conduct approximately " + AMT + r" in reinvestment purchases and an additional "
                r"approximately " + AMT + r" in reserve management purchases over the noted monthly period\."), True),
)


def _n(s: str):
    v = float(s)
    return int(v) if v == int(v) else v


def parse_sentence(s: str) -> tuple:
    """-> (reinv, rmp) in $ billions as the NY Fed states them."""
    for rx, has_rmp in FORMS:
        m = rx.fullmatch(s)
        if m:
            return _n(m.group(1)), (_n(m.group(2)) if has_rmp else 0)
    raise SourceError(f"purchase schedule: sentence not parsed: {s!r}")


def parse(html: bytes | str) -> dict:
    """The newest period -> {from, to, rmp, reinv}."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for t in soup.find_all("table"):
        rows = [[cell_text(c) for c in tr.find_all(["td", "th"])] for tr in t.find_all("tr")]
        if rows and rows[0] == HEADER:
            tables.append(rows)
    if len(tables) != 1:
        raise SourceError(f"purchase schedule: {len(tables)} tables with header {HEADER}")
    rows = [r for r in tables[0][1:] if len(r) >= 2]
    if not rows or not re.fullmatch(PERIOD, rows[0][0]):
        raise SourceError(f"purchase schedule: first period cell not parsed: {rows[0][0] if rows else None!r}")
    periods = []
    for r in rows:   # rows from March 2020 hold single dates ('4/01/2020'); only ranges are compared
        m = re.fullmatch(PERIOD, r[0])
        if m:
            periods.append((mdy(m.group(1)), mdy(m.group(2)), r[1]))
    a, b, s = max(periods, key=lambda p: p[0])
    if (a, b, s) != periods[0]:
        raise SourceError("purchase schedule: the newest period is not the first row")
    reinv, rmp = parse_sentence(s)
    return {"from": a.isoformat(), "to": b.isoformat(), "rmp": rmp, "reinv": reinv}


def get() -> dict:
    return parse(fetch(URL))
