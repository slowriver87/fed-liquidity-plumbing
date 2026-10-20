"""Federal Reserve H.4.1: the release page (Tables 1 and 5) and the release's own XML file.

Layouts printed on Oct 20, 2026 (tests/fixtures/README.md):
- HTML: each table is preceded by a numbered title paragraph ("1. Factors Affecting Reserve Balances of
  Depository Institutions", "... (continued)"; "5. Consolidated Statement of Condition of All Federal
  Reserve Banks"). Table 1 spans two HTML tables. Footnote markers are <span> elements styled
  vertical-align:super (not <sup>). Changes print as "+ 11,680" / "- 7,519" (ASCII hyphen); negative
  levels as "-26,334".
- XML: FRB_h41_xml.zip holds H41_data.xml (SDMX, about 127 MB) with weekly series named by code, e.g.
  RESH4R_XAW_N.WW (reserve balances, week average). Every value the page uses has its own series,
  including both change columns.
"""
from __future__ import annotations

import datetime as dt
import io
import re
import zipfile

from .common import MONTH_ABBR, SourceError, fetch

URL_CURRENT = "https://www.federalreserve.gov/releases/h41/current/"
URL_DATED = "https://www.federalreserve.gov/releases/h41/{d:%Y%m%d}/"
URL_XML = "https://www.federalreserve.gov/releases/h41/data/FRB_h41_xml.zip"

# DATA.h41.t1 keys -> (exact Table 1 label, parent row or None)
T1_ROWS = {
    "reserves": ("Reserve balances with Federal Reserve Banks", None),
    "tga": ("U.S. Treasury, General Account", None),
    "rrp": ("Reverse repurchase agreements", None),
    "rrpForeign": ("Foreign official and international accounts", "Reverse repurchase agreements"),
    "rrpOthers": ("Others", "Reverse repurchase agreements"),
    "loans": ("Loans", None),
    "primary": ("Primary credit", "Loans"),
    "repo": ("Repurchase agreements", None),
    "held": ("Securities held outright", None),
    "ust": ("U.S. Treasury securities", None),
    "bills": ("Bills", None),
    "mbs": ("Mortgage-backed securities", None),
}
# Labels repeat ("Others", "Foreign official"), so a sub-row is matched only inside its parent's block:
# the rows directly under the parent must be exactly these, in this order.
CHILDREN = {
    "Repurchase agreements": ("Foreign official", "Others"),
    "Loans": ("Primary credit", "Secondary credit", "Seasonal credit"),
    "Reverse repurchase agreements": ("Foreign official and international accounts", "Others"),
}
T1_TITLE = "Factors Affecting Reserve Balances of Depository Institutions"
T5_TITLE = "Consolidated Statement of Condition of All Federal Reserve Banks"
T5_ROW = "Total assets"


# ---------------------------------------------------------------------------
# HTML

def _date(mon: str, day: str, year: str) -> str:
    m = MONTH_ABBR.get(mon[:3].title())
    if not m:
        raise SourceError(f"H.4.1: unexpected month {mon!r}")
    return dt.date(int(year), m, int(day)).isoformat()


DATE_RE = r"([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4})"


def _num(s: str) -> int | None:
    """'565,492' / '+ 4,725' / '- 7,519' / '-26,334' / '0' -> int; '...' or blank -> None."""
    t = " ".join(s.replace("−", "-").split())
    m = re.fullmatch(r"([+-])? ?(\d{1,3}(?:,\d{3})*)", t)
    if not m:
        return None
    v = int(m.group(2).replace(",", ""))
    return -v if m.group(1) == "-" else v


def _label(td) -> str:
    """Cell text without footnote markers (spans styled vertical-align:super, or <sup>)."""
    import copy
    td = copy.copy(td)
    for el in td.find_all(["span", "sup"]):
        if el.name == "sup" or "vertical-align:super" in (el.get("style") or "").replace(" ", ""):
            el.decompose()
    return " ".join(td.get_text(" ").split())


def _titled_tables(soup) -> list[tuple[str, object]]:
    """[(title without number and '(continued)', table)] for every table that follows a numbered title."""
    out = []
    for t in soup.find_all("table"):
        p = t.find_previous(lambda e: e.name == "p" and e.find_parent("table") is None
                            and re.match(r"\d+[A-Z]?\.\s", " ".join(e.get_text(" ").split())))
        if p is None:
            continue
        title = " ".join(p.get_text(" ").split())
        title = re.sub(r"^\d+[A-Z]?\.\s+", "", title)
        title = re.sub(r"\s*\(continued\)$", "", title)
        out.append((title, t))
    return out


def _rows(tables, ncells: int) -> list[tuple[str, list]]:
    """[(label, [numbers])] for rows with exactly ncells cells and a non-empty label."""
    out = []
    for t in tables:
        for tr in t.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) != ncells:
                continue
            lab = _label(tds[0])
            if lab:
                out.append((lab, [_num(" ".join(td.get_text(" ").split())) for td in tds[1:]]))
    return out


def _find(rows, label: str, parent: str | None, where: str) -> list:
    labels = [r[0] for r in rows]
    if parent is None:
        hits = [i for i, l in enumerate(labels) if l == label]
        if len(hits) != 1:
            raise SourceError(f"H.4.1 {where}: row {label!r} found {len(hits)} times; rows: {labels}")
        return rows[hits[0]][1]
    ps = [i for i, l in enumerate(labels) if l == parent]
    if len(ps) != 1:
        raise SourceError(f"H.4.1 {where}: parent row {parent!r} found {len(ps)} times; rows: {labels}")
    kids = CHILDREN[parent]
    block = labels[ps[0] + 1:ps[0] + 1 + len(kids)]
    if tuple(block) != kids:
        raise SourceError(f"H.4.1 {where}: rows under {parent!r} are {block}, expected {list(kids)}")
    return rows[ps[0] + 1 + kids.index(label)][1]


def parse_html(html: bytes | str) -> dict:
    """-> {release, wed, prev, yago, t1: {key: [avg, chg, chg_yago, wed]}, assets: [wed, chg, chg_yago]}."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    m = re.search(r"Release Date: " + DATE_RE, text)
    if not m:
        raise SourceError("H.4.1: 'Release Date:' line not found")
    release = _date(*m.groups())

    titled = _titled_tables(soup)
    t1 = [t for title, t in titled if title == T1_TITLE]
    t5 = [t for title, t in titled if title == T5_TITLE]
    if not t1 or not t5:
        raise SourceError(f"H.4.1: table titles not found; titles: {sorted({x for x, _ in titled})}")

    head = " ".join(t1[0].get_text(" ").split())
    m = re.search(r"Wednesday " + DATE_RE + r".*?Week ended " + DATE_RE + r" Change from week ended "
                  + DATE_RE + " " + DATE_RE, head)
    if not m:
        raise SourceError(f"H.4.1 Table 1: header not recognised: {head[:300]!r}")
    g = m.groups()
    wed, week, prev, yago = _date(*g[0:3]), _date(*g[3:6]), _date(*g[6:9]), _date(*g[9:12])
    if wed != week:
        raise SourceError(f"H.4.1 Table 1: week ended {week} but Wednesday column {wed}")

    rows1 = _rows(t1, 5)
    out = {}
    for key, (label, parent) in T1_ROWS.items():
        v = _find(rows1, label, parent, "Table 1")
        if any(x is None for x in v):
            raise SourceError(f"H.4.1 Table 1: row {label!r} has unreadable values {v}")
        out[key] = v

    head5 = " ".join(t5[0].get_text(" ").split())
    m = re.search(r"Eliminations from consolidation Wednesday " + DATE_RE + r" Change since Wednesday Wednesday "
                  + DATE_RE + " " + DATE_RE, head5)
    if not m:
        raise SourceError(f"H.4.1 Table 5: header not recognised: {head5[:300]!r}")
    g = m.groups()
    if (_date(*g[0:3]), _date(*g[3:6]), _date(*g[6:9])) != (wed, prev, yago):
        raise SourceError(f"H.4.1 Table 5: header dates {g} do not match Table 1 ({wed}, {prev}, {yago})")
    v = _find(_rows(t5, 5), T5_ROW, None, "Table 5")
    assets = v[1:]   # v[0] is the eliminations column, printed "(0)"
    if any(x is None for x in assets):
        raise SourceError(f"H.4.1 Table 5: row {T5_ROW!r} has unreadable values {v}")
    return {"release": release, "wed": wed, "prev": prev, "yago": yago, "t1": out, "assets": assets}


def release_date_for(wed: str) -> dt.date:
    """The Thursday after the Wednesday, or the next business day when that Thursday is a holiday."""
    from .common import is_business_day
    d = dt.date.fromisoformat(wed) + dt.timedelta(1)
    while not is_business_day(d):
        d += dt.timedelta(1)
    return d


def get_html(release: dt.date | None = None) -> dict:
    url = URL_CURRENT if release is None else URL_DATED.format(d=release)
    return parse_html(fetch(url))


# ---------------------------------------------------------------------------
# XML (series codes and descriptions printed from the Oct 15, 2026 file)

_COLS = (("_XAW", "week average"),
         ("_XAW_XCH1", "change in week average from previous week average"),
         ("_XAW_XCH52", "change in week average from year ago week average"),
         ("", "Wednesday level"))
_ASSET_COLS = (("", "Wednesday level"),
               ("_XCH1", "change in Wednesday level from previous Wednesday level"),
               ("_XCH52", "change in Wednesday level from year ago level"))
SERIES_ROWS = {
    "reserves": ("RESH4R", "Liabilities and Capital: Other Factors Draining Reserve Balances: "
                           "Reserve balances with Federal Reserve Banks"),
    "tga": ("RESPPLLDT", "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, other than reserve "
                         "balances: U.S. Treasury, General Account"),
    "rrp": ("RESPPLLR", "Liabilities and Capital: Liabilities: Reverse repurchase agreements"),
    "rrpForeign": ("RESPPLLRF", "Liabilities and Capital: Liabilities: Reverse repurchase agreements: "
                                "Foreign official and international accounts"),
    "rrpOthers": ("RESPPLLRD", "Liabilities and Capital: Liabilities: Reverse repurchase agreements: Others"),
    "loans": ("RESPPALD", "Assets: Liquidity and Credit Facilities: Loans"),
    "primary": ("RESPPALDP", "Assets: Liquidity and Credit Facilities: Loans: Primary credit"),
    "repo": ("RESPPALGTR", "Assets: Other: Repurchase agreements"),
    "held": ("RESPPALG", "Assets: Securities Held Outright: Securities held outright"),
    "ust": ("RESPPALGUO", "Assets: Securities Held Outright: U.S. Treasury securities"),
    "bills": ("RESPPALGUOB", "Assets: Securities Held Outright: U.S. Treasury securities: Bills"),
    "mbs": ("RESPPALGASMO", "Assets: Securities Held Outright: Mortgage-backed securities"),
}
SERIES_ASSETS = ("RESPPMA", "Assets: Total Assets: Total assets (Less eliminations from consolidation)")


def series_map() -> dict[str, tuple[str, int, str]]:
    """{SERIES_NAME: (key, column, exact short description)}."""
    out = {}
    for key, (code, desc) in SERIES_ROWS.items():
        for j, (suf, what) in enumerate(_COLS):
            out[f"{code}{suf}_N.WW"] = (key, j, f"{desc}: {what}")
    code, desc = SERIES_ASSETS
    for j, (suf, what) in enumerate(_ASSET_COLS):
        out[f"{code}{suf}_N.WW"] = ("assets", j, f"{desc}: {what}")
    return out


def parse_xml(source, since: str = "2000-01-01") -> dict[str, dict]:
    """H41_data.xml (path, bytes or file object) -> {wed: {key: [4 values], 'assets': [3 values]}} for
    Wednesdays on or after `since`. A value the file does not hold is None."""
    import xml.etree.ElementTree as ET
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    smap = series_map()
    seen, out = {}, {}
    try:
        for _, el in ET.iterparse(source, events=("end",)):
            if not el.tag.endswith("}Series"):
                continue
            name = el.get("SERIES_NAME")
            if name in smap:
                key, j, want = smap[name]
                desc = next((a.findtext("{*}AnnotationText") for a in el.iter()
                             if a.tag.endswith("}Annotation") and a.findtext("{*}AnnotationType") == "Short Description"), None)
                if desc != want:
                    raise SourceError(f"H.4.1 XML: {name} is {desc!r}, expected {want!r}")
                if el.get("UNIT_MULT") != "1000000":
                    raise SourceError(f"H.4.1 XML: {name} UNIT_MULT {el.get('UNIT_MULT')}")
                seen[name] = True
                for ob in el.iter():
                    if not ob.tag.endswith("}Obs") or ob.get("TIME_PERIOD", "") < since:
                        continue
                    v = ob.get("OBS_VALUE")
                    ok = ob.get("OBS_STATUS") == "A" and v not in (None, "", "-999999", "-9999")
                    n = len(_ASSET_COLS) if key == "assets" else len(_COLS)
                    row = out.setdefault(ob.get("TIME_PERIOD"), {}).setdefault(key, [None] * n)
                    row[j] = int(round(float(v))) if ok else None
            el.clear()
    except ET.ParseError as e:
        raise SourceError(f"H.4.1 XML: {e}")
    missing = sorted(set(smap) - set(seen))
    if missing:
        raise SourceError(f"H.4.1 XML: series not found: {missing}")
    return out


def get_xml(since: str) -> dict[str, dict]:
    content = fetch(URL_XML)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if "H41_data.xml" not in z.namelist():
                raise SourceError(f"H.4.1 XML zip: H41_data.xml not found; files: {z.namelist()}")
            with z.open("H41_data.xml") as f:
                return parse_xml(f, since)
    except zipfile.BadZipFile as e:
        raise SourceError(f"H.4.1 XML zip: {e}")


def xml_week(xml: dict[str, dict], wed: str) -> dict | None:
    """One Wednesday from parse_xml in the shape of parse_html's t1/assets, or None if absent."""
    w = xml.get(wed)
    if not w:
        return None
    return {"t1": {k: w.get(k, [None] * 4) for k in T1_ROWS}, "assets": w.get("assets", [None] * 3)}
