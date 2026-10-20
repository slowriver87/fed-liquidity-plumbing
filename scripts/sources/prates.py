"""Federal Reserve policy rates: the IORB series from the PRATES release file, and the FOMC implementation
note as a second read on decision days.

Layouts printed Oct 20, 2026:
- FRB_PRATES_xml.zip holds PRATES_data.xml (SDMX). Series RESBM_N.D, short description "Interest rate on
  reserve balances (IORB rate), Last Changed 09-17-2026", is daily (weekends included), `OBS_VALUE="3.90"`.
  The other two series (IOER, IORR) end in 2021.
- Implementation note: "... voted unanimously to raise the interest rate paid on reserve balances to 3.90
  percent, effective September 17, 2026." and the directive "... target range of 3-3/4 to 4 percent.
  Conduct standing overnight repurchase agreement operations at a rate of 4.0 percent [Oct 2025 form: with a
  minimum bid rate of 4.0 percent ...]. Conduct standing overnight reverse repurchase agreement operations at
  an offering rate of 3.75 percent ...".
"""
from __future__ import annotations

import io
import re
import zipfile

from .common import MONTH_NAME, SourceError, fetch, visible_text

URL_XML = "https://www.federalreserve.gov/releases/prates/data/FRB_PRATES_xml.zip"
URL_NOTE = "https://www.federalreserve.gov/newsevents/pressreleases/monetary{d:%Y%m%d}a1.htm"
SERIES = "RESBM_N.D"
DESC_RE = r"Interest rate on reserve balances \(IORB rate\)(, Last Changed \d{2}-\d{2}-\d{4})?"


def parse_xml(source) -> dict[str, float]:
    """-> {date: IORB %} for every observation of RESBM_N.D."""
    import xml.etree.ElementTree as ET
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    out, found = {}, False
    try:
        for _, el in ET.iterparse(source, events=("end",)):
            if not el.tag.endswith("}Series"):
                continue
            if el.get("SERIES_NAME") == SERIES:
                found = True
                desc = next((a.findtext("{*}AnnotationText") for a in el.iter()
                             if a.tag.endswith("}Annotation") and a.findtext("{*}AnnotationType") == "Short Description"), "")
                if not re.fullmatch(DESC_RE, desc or ""):
                    raise SourceError(f"policy rates XML: {SERIES} is {desc!r}")
                for ob in el.iter():
                    if ob.tag.endswith("}Obs") and ob.get("OBS_STATUS") == "A":
                        out[ob.get("TIME_PERIOD")] = float(ob.get("OBS_VALUE"))
            el.clear()
    except (ET.ParseError, TypeError, ValueError) as e:
        raise SourceError(f"policy rates XML: {e}")
    if not found or not out:
        raise SourceError(f"policy rates XML: series {SERIES} not found")
    return out


def get_xml() -> dict[str, float]:
    content = fetch(URL_XML)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if "PRATES_data.xml" not in z.namelist():
                raise SourceError(f"policy rates zip: PRATES_data.xml not found; files: {z.namelist()}")
            with z.open("PRATES_data.xml") as f:
                return parse_xml(f)
    except zipfile.BadZipFile as e:
        raise SourceError(f"policy rates zip: {e}")


def in_force(series: dict[str, float], date: str) -> float | None:
    """IORB in force on `date`: the observation for that date, else the latest before it."""
    if date in series:
        return series[date]
    prior = [d for d in series if d <= date]
    return series[max(prior)] if prior else None


# ---------------------------------------------------------------------------
# Implementation note

_DATE = r"([A-Z][a-z]+) (\d{1,2}), (\d{4})"
_FRAC = r"(\d+(?:-\d/\d)?|\d/\d)"


def _iso(mon: str, day: str, year: str) -> str:
    if mon not in MONTH_NAME:
        raise SourceError(f"implementation note: unexpected month {mon!r}")
    return f"{int(year):04d}-{MONTH_NAME[mon]:02d}-{int(day):02d}"


def _frac(s: str) -> float:
    """'3-3/4' -> 3.75; '4' -> 4.0; '1/4' -> 0.25."""
    m = re.fullmatch(r"(?:(\d+)-)?(\d)/(\d)", s)
    if m:
        return int(m.group(1) or 0) + int(m.group(2)) / int(m.group(3))
    return float(s)


NOTE_PATTERNS = {
    "iorb": r"interest rate paid on reserve balances (?:to|at) (\d+\.\d+) percent, effective " + _DATE,
    "range": r"federal funds rate in a target range of " + _FRAC + r" to " + _FRAC + r" percent",
    "srf": r"Conduct standing overnight repurchase agreement operations (?:at a rate|with a minimum bid rate) of (\d+\.\d+) percent",
    "rrp": r"Conduct standing overnight reverse repurchase agreement operations at an offering rate of (\d+\.\d+) percent",
    "statement": r"announced by the Federal Open Market Committee in its statement on " + _DATE,
}


def parse_note(text: str) -> dict:
    """-> {statement, effective, iorb, lo, hi, srf, rrp}. Every pattern must match exactly once."""
    t = " ".join(text.split())
    hits = {}
    for k, pat in NOTE_PATTERNS.items():
        ms = re.findall(pat, t)
        if len(ms) != 1:
            raise SourceError(f"implementation note: {k!r} sentence found {len(ms)} times")
        hits[k] = ms[0]
    iorb, mon, day, year = hits["iorb"]
    return {"statement": _iso(*hits["statement"]), "effective": _iso(mon, day, year), "iorb": float(iorb),
            "lo": _frac(hits["range"][0]), "hi": _frac(hits["range"][1]),
            "srf": float(hits["srf"]), "rrp": float(hits["rrp"])}


def get_note(decision_day) -> dict:
    return parse_note(visible_text(fetch(URL_NOTE.format(d=decision_day))))
