"""BEA: core PCE price index (NIPA table T20804, monthly) through the API, and the release-dates file.

Copied from the Yield Regime Monitor's sources/bea.py (its runner dry run read T20804 with a real key),
trimmed to the core PCE line. The API could not be read from the setup environment (no BEA_API_KEY), so
the T20804 fixture is the Yield repo's documentation-based sample. Line labels are matched exactly; when
one is missing, the error lists the table's labels so the run summary shows what to map.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from .common import SourceError, fetch, month_add, num

URL_API = ("https://apps.bea.gov/api/data/?UserID={k}&method=GetData&DataSetName=NIPA&TableName={t}"
           "&Frequency={f}&Year={y}&ResultFormat=JSON")
URL_DATES = "https://apps.bea.gov/API/signup/release_dates.json"
CORE_LINE = ("T20804", "PCE excluding food and energy")


def parse_api(text: str) -> list[dict]:
    """BEAAPI.Results.Data[] rows -> [{line, label, period, value}] in table order.
    Periods: '2026Q2'; monthly '2026M8' or '2026M08' -> '2026-08'. DataValue has thousands commas."""
    try:
        d = json.loads(text)["BEAAPI"]
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError(f"BEA API: {e}")
    res = d.get("Results") or {}
    if isinstance(res, list):
        res = res[0] if res else {}
    err = res.get("Error") or d.get("Error")
    if err:
        raise SourceError(f"BEA API error {err.get('APIErrorCode')}: {err.get('APIErrorDescription')}")
    rows = []
    for r in res.get("Data") or []:
        p = r.get("TimePeriod", "")
        m = re.fullmatch(r"(\d{4})M(\d{1,2})", p)
        if m:
            p = f"{m.group(1)}-{int(m.group(2)):02d}"
        elif not re.fullmatch(r"\d{4}Q[1-4]", p):
            raise SourceError(f"BEA API: unexpected TimePeriod {p!r}")
        rows.append({"line": int(r["LineNumber"]), "label": " ".join(str(r["LineDescription"]).split()),
                     "period": p, "value": num(r.get("DataValue"))})
    if not rows:
        raise SourceError("BEA API: no data rows")
    return rows


def labels(rows: list[dict]) -> list[str]:
    seen = {}
    for r in rows:
        seen.setdefault(r["line"], r["label"])
    return [f"{k}: {v}" for k, v in sorted(seen.items())]


def series(rows: list[dict], label: str, table: str = "") -> dict[str, float]:
    """{period: value} for the line with this exact label."""
    lines = sorted({r["line"]: r["label"] for r in rows}.items())
    hits = [n for n, lab in lines if lab == label]
    if len(hits) != 1:
        raise SourceError(f"BEA {table}: line {label!r} found {len(hits)} times; lines: {labels(rows)}")
    return {r["period"]: r["value"] for r in rows if r["line"] == hits[0] and r["value"] is not None}


def pce(rows: list[dict]) -> dict:
    """Core PCE price index, y/y % from levels 12 months apart, rounded to 1 decimal at the end.
    -> {month, v} for the newest month."""
    core = series(rows, CORE_LINE[1], table=CORE_LINE[0])
    cy = {m: round((v / core[month_add(m, -12)] - 1) * 100, 1) for m, v in core.items() if core.get(month_add(m, -12))}
    if not cy:
        raise SourceError("BEA T20804: fewer than 13 months of core PCE")
    m = max(cy)
    return {"month": m, "v": cy[m], "all": cy}


def get_table(api_key: str, today: dt.date) -> list[dict]:
    if not api_key:
        raise SourceError("BEA API: no BEA_API_KEY")
    url = URL_API.format(k=api_key, t=CORE_LINE[0], f="M", y=f"{today.year - 2},{today.year - 1},{today.year}")
    return parse_api(fetch(url, label=f"BEA API {CORE_LINE[0]}").decode("utf-8"))


# ---------------------------------------------------------------------------
# Release dates (verified): {"Personal Income and Outlays": {"release_dates": [ISO UTC, ...]}, ...}

def parse_dates(text: str) -> list[dict]:
    """[{d, hm, ref}] for Personal Income and Outlays, converted to ET; ref = the month the release covers."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    try:
        d = json.loads(text)
    except ValueError as e:
        raise SourceError(f"BEA release dates: {e}")
    name = "Personal Income and Outlays"
    if name not in d:
        raise SourceError(f"BEA release dates: {name!r} not found")
    out = []
    for s in sorted(set(d[name]["release_dates"])):
        t = dt.datetime.fromisoformat(s).astimezone(et)
        out.append({"d": t.date().isoformat(), "hm": (t.hour, t.minute), "ref": month_add(t.strftime("%Y-%m"), -1)})
    return out


def get_dates() -> list[dict]:
    return parse_dates(fetch(URL_DATES).decode("utf-8"))
