"""Shared helpers for the source modules: fetching, errors, dates and text."""
from __future__ import annotations

import calendar
import datetime as dt
import re
import time

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 60
RETRY_WAITS = (5, 15, 45)

MONTH_NAME = {n: i for i, n in enumerate(calendar.month_name) if n}
MONTH_ABBR = {a: i for i, a in enumerate(calendar.month_abbr) if a}


class CheckFailed(Exception):
    """A publishing check failed: change nothing, fail the run."""


class SourceError(Exception):
    """A source could not be fetched or read."""


class NotFound(SourceError):
    """The server answered 404: the file is not published (yet)."""


# ---------------------------------------------------------------------------
# Fetching

def _request(method: str, url: str, timeout: int, **kw):
    import requests
    headers = {"User-Agent": USER_AGENT, **kw.pop("headers", {})}
    return requests.request(method, url, headers=headers, timeout=timeout, **kw)


def fetch(url: str, timeout: int = TIMEOUT, waits=RETRY_WAITS, sleep=None,
          method: str = "GET", label: str | None = None, **kw) -> bytes:
    """One try plus a retry after each wait. A 404 is not retried. `label` replaces the URL in
    error messages, so a URL that carries an API key is never printed."""
    import requests
    last = None
    for attempt in range(len(waits) + 1):
        try:
            r = _request(method, url, timeout, **kw)
            # A 404, or a redirect to an error page (the New York Fed answers 302 -> /errors/404)
            if r.status_code == 404 or re.search(r"/errors?/404\b", r.url or ""):
                raise NotFound(f"{label or url}: 404 Not Found")
            r.raise_for_status()
            return r.content
        except NotFound:
            raise
        except requests.RequestException as e:
            last = type(e).__name__ if label else e
            if isinstance(e, requests.HTTPError) and e.response is not None:
                last = f"HTTP {e.response.status_code}"
            if attempt < len(waits):
                (sleep or time.sleep)(waits[attempt])
    raise SourceError(f"{label or url}: {last}")


# ---------------------------------------------------------------------------
# Dates

def iso(d: dt.date) -> str:
    return d.isoformat()


def parse_iso(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def mdy(s: str) -> dt.date:
    """'10/16/2026' or '4/2/1971' -> date."""
    m, d, y = (int(x) for x in s.strip().split("/"))
    return dt.date(y, m, d)


def month_add(m: str, n: int) -> str:
    y, mo = map(int, m.split("-"))
    k = y * 12 + (mo - 1) + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def quarter_add(q: str, n: int) -> str:
    y, k = int(q[:4]), int(q[5])
    t = y * 4 + (k - 1) + n
    return f"{t // 4}Q{t % 4 + 1}"


def month_of(name: str, year: int | str) -> str:
    return f"{int(year):04d}-{MONTH_NAME[name]:02d}"


def mon_abbr(m: str) -> str:
    """'2026-09' -> 'Sep'."""
    return calendar.month_abbr[int(m[5:7])]


def day_label(d: str) -> str:
    """'2026-10-16' -> 'Oct 16'."""
    return f"{calendar.month_abbr[int(d[5:7])]} {int(d[8:10])}"


def _nth_weekday(y: int, m: int, wd: int, n: int) -> dt.date:
    if n > 0:
        d = dt.date(y, m, 1)
        d += dt.timedelta((wd - d.weekday()) % 7)
        return d + dt.timedelta(7 * (n - 1))
    d = dt.date(y, m, calendar.monthrange(y, m)[1])
    return d - dt.timedelta((d.weekday() - wd) % 7)


def _observed(d: dt.date) -> dt.date:
    if d.weekday() == 5:
        return d - dt.timedelta(1)
    if d.weekday() == 6:
        return d + dt.timedelta(1)
    return d


def federal_holidays(y: int) -> set[dt.date]:
    """US federal holidays (observed dates)."""
    return {
        _observed(dt.date(y, 1, 1)), _nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3),
        _nth_weekday(y, 5, 0, -1), _observed(dt.date(y, 6, 19)), _observed(dt.date(y, 7, 4)),
        _nth_weekday(y, 9, 0, 1), _nth_weekday(y, 10, 0, 2), _observed(dt.date(y, 11, 11)),
        _nth_weekday(y, 11, 3, 4), _observed(dt.date(y, 12, 25)),
        _observed(dt.date(y + 1, 1, 1)),  # Jan 1 on a Saturday is observed on Dec 31
    }


def is_business_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in federal_holidays(d.year)


def add_business_days(d: dt.date, n: int) -> dt.date:
    while n > 0:
        d += dt.timedelta(1)
        if is_business_day(d):
            n -= 1
    return d


def business_days_between(a: dt.date, b: dt.date) -> int:
    n, d = 0, a
    while d < b:
        d += dt.timedelta(1)
        if is_business_day(d):
            n += 1
    return n


def time_label(hour: int, minute: int) -> str:
    """(8, 30) -> '8:30 am'; (14, 0) -> '2:00 pm'."""
    h = hour % 12 or 12
    return f"{h}:{minute:02d} {'am' if hour < 12 else 'pm'}"


def parse_time_label(s: str) -> tuple[int, int]:
    """'8:30 AM' / '01:00 PM' / '10am' -> (hour, minute) in 24h."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?\s*[Mm]\.?\s*", s)
    if not m:
        raise SourceError(f"unexpected time {s!r}")
    h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
    return (h + 12 if m.group(3).lower() == "p" else h), mi


# ---------------------------------------------------------------------------
# Text

def visible_text(html: bytes | str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return " ".join(soup.get_text(" ").split())


def cell_text(el) -> str:
    return " ".join(el.get_text(" ").split())


def num(s) -> float | None:
    """'1,234.5' -> 1234.5; blanks and markers ('', '.', '-', 'ND', '(NA)') -> None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace(",", "")
    if not re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)", t):
        return None
    return float(t)
