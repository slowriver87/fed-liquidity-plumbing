#!/usr/bin/env python3
"""Refresh the Fed Liquidity Plumbing data blocks from public sources.

Fetches each source, rewrites the seven script-written lines in index.html (DATA, WEEKS, RATES, OPS,
POLICY, CAL, LOG), runs the checks from HANDOFF.md, writes data/summary.json and lists the alert issues
to open. NOTES, AUDIT and everything else in the page are never touched. The workflow commits only if
this script exits 0 and reports changed=true.

Exit codes: 0 = updated, or nothing new; 1 = a check failed (nothing written).
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources import bea, dts, fomc, h41, nyfed, prates, rmp  # noqa: E402
from sources import calendar as cal  # noqa: E402
from sources.common import CheckFailed, SourceError, day_label, is_business_day, parse_iso  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
START_MARK = "// ===== DATA BLOCKS"
END_MARK = "// ===== END DATA BLOCKS ====="
BLOCK_NAMES = ("DATA", "WEEKS", "RATES", "OPS", "POLICY", "CAL", "LOG", "NOTES", "AUDIT")
WRITTEN = ("DATA", "WEEKS", "RATES", "OPS", "POLICY", "CAL", "LOG")
FIXED = ("NOTES", "AUDIT")
SEED_MARK = "Seeded"
SEED_PENDING = "not re-checked"
MINUS = "−"
EN_DASH = "–"
N_WEEKS, N_DAYS = 17, 10
PCT_MOVE = 5.0          # percent, for amounts
RATE_MOVE = 0.10        # percentage points, for rates
SMALL_AMOUNT = 5000     # $M: amounts below $5B are exempt from the 5% rule
LEVEL_MOVE_FIELDS = {"WEEKS.res", "WEEKS.tga", "WEEKS.assets", "WEEKS.netliq"}
LEVEL_MOVE_PCT = {"WEEKS.tga": 15.0}  # percent: the Treasury account swings 5-10% a week around tax dates
RESERVES_ALERT = 100_000
SRF_ALERT = 25_000
LOOKBACK_DAYS = 35
OPS_DAY_DONE = (14, 0)  # ET: a day's operations count once the 13:30 operation has closed

# LOG rows (exact indicator labels): the source whose seed check the row carries, then the other sources
# whose new data also rewrite the row.
LOG_ROWS = {
    "Bank reserves": ("h41", ()),
    "Treasury General Account": ("h41", ()),
    "Treasury account, daily": ("dts", ()),
    "Overnight reverse repo": ("ops", ()),
    "Fed total assets, y/y": ("h41", ()),
    "Net liquidity, 13-wk change": ("h41", ()),
    "SOFR minus IORB": ("rates", ()),
    "Fed funds minus IORB": ("rates", ()),
    "Standing repo facility usage": ("ops", ()),
    "Fed loans to banks": ("h41", ()),
    "Real fed funds rate": ("bea", ("rates",)),
    "Fed funds target range": ("rates", ()),
    "IORB": ("prates", ()),
    "Standing repo rate": ("ops", ()),
    "Overnight reverse repo rate": ("ops", ()),
    "Treasury bills held, y/y": ("h41", ()),
    "Mortgage bonds held, y/y": ("h41", ()),
    "Reserve management purchases": ("rmp", ()),
    "Next FOMC meeting": ("fomc", ()),
}
LOG_SOURCE_TEXT = {"IORB": "Fed policy rates file (PRATES)"}
SOURCE_NAMES = {"h41": "Fed H.4.1", "dts": "Treasury DTS", "ops": "NY Fed operation results",
                "rates": "NY Fed SOFR/EFFR", "prates": "Fed policy rates file", "bea": "BEA",
                "rmp": "NY Fed purchase schedule", "fomc": "Fed FOMC calendar", "bea_dates": "BEA release dates"}


# ---------------------------------------------------------------------------
# The page: nine one-line data blocks, seven of them written here

def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def block_lines(lines: list[str]) -> dict[str, int]:
    starts = [i for i, l in enumerate(lines) if l.startswith(START_MARK)]
    ends = [i for i, l in enumerate(lines) if l.rstrip("\r\n") == END_MARK]
    if len(starts) != 1 or len(ends) != 1 or ends[0] - starts[0] != len(BLOCK_NAMES) + 1:
        raise CheckFailed(f"data block markers not found, or not exactly {len(BLOCK_NAMES)} lines between them")
    idx = {}
    for i in range(starts[0] + 1, ends[0]):
        m = re.match(r"const (\w+) = ", lines[i])
        if not m or m.group(1) not in BLOCK_NAMES or m.group(1) in idx:
            raise CheckFailed(f"unexpected line {i + 1} between the data markers")
        idx[m.group(1)] = i
    return idx


def parse_block(line: str):
    body = line.rstrip("\r\n")
    if not body.endswith(";"):
        raise CheckFailed("data line does not end with ';'")
    return json.loads(body[body.index("=") + 1:-1].strip())


def load_page(html: str) -> dict:
    lines = html.splitlines(keepends=True)
    return {n: parse_block(lines[i]) for n, i in block_lines(lines).items()}


def render_page(html: str, blocks: dict) -> str:
    """Replace only the seven script-written lines."""
    lines = html.splitlines(keepends=True)
    idx = block_lines(lines)
    for name in WRITTEN:
        old = lines[idx[name]]
        ending = old[len(old.rstrip("\r\n")):]
        lines[idx[name]] = f"const {name} = {dump(blocks[name])};{ending}"
    return "".join(lines)


def extract_script(html: str) -> str:
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S | re.I)
    if len(scripts) != 1:
        raise CheckFailed(f"expected one <script>, found {len(scripts)}")
    return scripts[0]


def _node(path: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["node", *args, path], capture_output=True, text=True)
    except FileNotFoundError:
        raise CheckFailed("check 7: node not found; cannot check the page script")


def node_check(html: str, blocks: dict) -> None:
    """Check 7: the script parses, and Node reads the nine blocks back as the same values."""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "page.js")
        Path(p).write_text(extract_script(html), encoding="utf-8")
        r = _node(p, "--check")
        if r.returncode != 0:
            raise CheckFailed("check 7: page script does not parse: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        lines = html.splitlines(keepends=True)
        idx = block_lines(lines)
        js = "".join(lines[idx[n]] for n in BLOCK_NAMES)
        js += "process.stdout.write(JSON.stringify({" + ",".join(BLOCK_NAMES) + "}));\n"
        q = os.path.join(tmp, "blocks.js")
        Path(q).write_text(js, encoding="utf-8")
        r = _node(q)
        if r.returncode != 0:
            raise CheckFailed("check 7: data blocks do not evaluate: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        if json.loads(r.stdout) != blocks:
            raise CheckFailed("check 7: Node reads the data blocks differently")


def verify_page(before: str, after: str, blocks: dict) -> None:
    """Checks 5 and 7: with the seven script lines removed the page is identical; NOTES and AUDIT are
    byte-identical; the script parses and the blocks read back unchanged."""
    b, a = before.splitlines(keepends=True), after.splitlines(keepends=True)
    ib, ia = block_lines(b), block_lines(a)
    if ib != ia or len(a) != len(b):
        raise CheckFailed("check 5: page line structure changed")
    skip = {ib[n] for n in WRITTEN}
    diff = [i + 1 for i in range(len(b)) if i not in skip and a[i] != b[i]]
    if diff:
        raise CheckFailed(f"check 5: lines outside the seven data lines changed: {diff[:10]}")
    for n in FIXED:
        if a[ia[n]].encode("utf-8") != b[ib[n]].encode("utf-8"):
            raise CheckFailed(f"check 5: {n} changed")
    node_check(after, blocks)


# ---------------------------------------------------------------------------
# Numbers as the page prints them

def isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def js_round(x: float) -> int:
    """JavaScript Math.round (halves round up, also for negatives)."""
    return math.floor(x + 0.5)


def scaled(m, div: int, dp: int) -> str:
    """$ millions / div to dp decimals, halves rounded away from zero (exact on the decimal value, as the
    page's own rounding of e.g. 11,350 to "11.4" intends)."""
    from decimal import ROUND_HALF_UP, Decimal
    return str((Decimal(str(m)) / div).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


def usd_b(m, dp=0) -> str:
    return f"${scaled(m, 1000, dp)}B" if isnum(m) else "n/a"


def usd_t(m, dp=2) -> str:
    return f"${scaled(m, 1000000, dp)}T" if isnum(m) else "n/a"


def sgn_b(m, dp=0) -> str:
    return f"{'+' if m >= 0 else MINUS}${scaled(abs(m), 1000, dp)}B" if isnum(m) else "n/a"


def bp_s(v) -> str:
    return f"{'+' if v > 0 else MINUS if v < 0 else ''}{abs(v)}bp" if isnum(v) else "n/a"


def pct(v, dp=2) -> str:
    return f"{v:.{dp}f}%" if isnum(v) else "n/a"


def to_bp(a, b):
    return js_round((a - b) * 100) if isnum(a) and isnum(b) else None


def last_num(a: list) -> tuple:
    for i in range(len(a) - 1, -1, -1):
        if isnum(a[i]):
            return a[i], i
    return None, -1


# ---------------------------------------------------------------------------
# Tiles: the page's IND rules, ported (a test runs the page's own status() on the same blocks)

LABEL = {"good": "Ample", "warn": "Tightening", "crit": "Stressed", "none": "Not known"}


def derived(b: dict) -> dict:
    D, W, R, O, P = b["DATA"], b["WEEKS"], b["RATES"], b["OPS"], b["POLICY"]
    n = len(W["dates"])
    nl = [W["assets"][i] - W["tga"][i] - W["rrp"][i] if all(isnum(W[k][i]) for k in ("assets", "tga", "rrp")) else None
          for i in range(n)]
    sofr, si = last_num(R["sofr"])
    effr, ei = last_num(R["effr"])
    iorb_at = lambda i: R["iorb"][i] if isnum(R["iorb"][i]) else P["iorb"]
    srf, oi = last_num(O["srf"])
    onrrp, ri = last_num(O["rrp"])
    return {
        "netliq": nl, "nl_now": nl[-1] if n else None, "nl13": nl[n - 14] if n > 13 else None,
        "nl13_date": W["dates"][n - 14] if n > 13 else None,
        "sofr": sofr, "sofr_date": R["dates"][si] if si >= 0 else None, "sofr_iorb": iorb_at(si) if si >= 0 else None,
        "effr": effr, "effr_date": R["dates"][ei] if ei >= 0 else None, "effr_iorb": iorb_at(ei) if ei >= 0 else None,
        "gap_s": to_bp(sofr, iorb_at(si)) if si >= 0 else None, "gap_e": to_bp(effr, iorb_at(ei)) if ei >= 0 else None,
        "srf": srf, "srf_date": O["dates"][oi] if oi >= 0 else None,
        "onrrp": onrrp, "rrp_date": O["dates"][ri] if ri >= 0 else None,
        "real": effr - D["pce"]["v"] if isnum(effr) and isnum(D["pce"]["v"]) else None,
    }


def tiles(b: dict) -> list[dict]:
    D = b["DATA"]
    H, t1 = D["h41"], D["h41"]["t1"]
    x = derived(b)
    B = lambda v: v / 1e3 if isnum(v) else None
    T = lambda v: v / 1e6 if isnum(v) else None
    nl = js_round(B(x["nl_now"] - x["nl13"])) if isnum(x["nl_now"]) and isnum(x["nl13"]) else None
    inf = math.inf
    spec = [
        ("Bank reserves", T(t1["reserves"][0]), [(2.85, "crit"), (3.0, "warn"), (inf, "good")], None, H["wed"]),
        ("Treasury General Account", B(t1["tga"][0]), [(900, "good"), (1000, "warn"), (inf, "crit")], None, H["wed"]),
        ("Overnight reverse repo", B(x["onrrp"]), [(50, "warn"), (inf, "good")], {"warn": "Empty", "good": "Buffer"},
         x["rrp_date"]),
        ("Fed total assets, y/y", B(H["assets"][2]), [(-200, "crit"), (0, "warn"), (inf, "good")], None, H["wed"]),
        ("Net liquidity, 13-wk change", nl, [(-200, "crit"), (-25, "warn"), (inf, "good")], None,
         b["WEEKS"]["dates"][-1] if b["WEEKS"]["dates"] else None),
        ("SOFR minus IORB", x["gap_s"], [(0, "good"), (5, "warn"), (inf, "crit")], None, x["sofr_date"]),
        ("Fed funds minus IORB", x["gap_e"], [(0, "good"), (5, "warn"), (inf, "crit")], None, x["effr_date"]),
        ("Standing repo facility usage", B(x["srf"]), [(5, "good"), (25, "warn"), (inf, "crit")], None, x["srf_date"]),
        ("Fed loans to banks", B(t1["loans"][0]), [(10, "good"), (30, "warn"), (inf, "crit")], None, H["wed"]),
        ("Real fed funds rate", js_round(x["real"] * 10) / 10 if isnum(x["real"]) else None,
         [(0, "good"), (1.5, "warn"), (inf, "crit")], {"good": "Easy", "warn": "Restrictive", "crit": "Tight"},
         x["effr_date"]),
    ]
    out = []
    for name, value, zones, lab, asof in spec:
        if not isnum(value):
            st = "none"
        else:
            st = next((s for to, s in zones if value < to), zones[-1][1])
        out.append({"name": name, "status": st, "label": (lab or {}).get(st) or LABEL[st], "value": value, "asof": asof})
    return out


def build_summary(b: dict) -> dict:
    D, P = b["DATA"], b["POLICY"]
    x = derived(b)
    r1 = lambda v: round(v, 1) if isnum(v) else None
    r3 = lambda v: round(v, 3) if isnum(v) else None
    t1 = D["h41"]["t1"]
    return {
        "updated": D["updated"], "h41_week": D["h41"]["wed"], "rates_date": x["sofr_date"],
        "reserves_T": r3(t1["reserves"][0] / 1e6) if isnum(t1["reserves"][0]) else None,
        "tga_B": r1(t1["tga"][0] / 1e3) if isnum(t1["tga"][0]) else None,
        "netliq_T": r3(x["nl_now"] / 1e6) if isnum(x["nl_now"]) else None,
        "netliq_13w_B": js_round((x["nl_now"] - x["nl13"]) / 1e3) if isnum(x["nl_now"]) and isnum(x["nl13"]) else None,
        "sofr": x["sofr"], "effr": x["effr"], "iorb": P["iorb"], "sofr_iorb_bp": x["gap_s"], "effr_iorb_bp": x["gap_e"],
        "srf_B": round(x["srf"] / 1e3, 3) if isnum(x["srf"]) else None,
        "onrrp_B": r1(x["onrrp"] / 1e3) if isnum(x["onrrp"]) else None,
        "range": [P["lo"], P["hi"]], "rmp": P["rmp"],
        "tiles": [{"name": t["name"], "status": t["status"], "label": t["label"]} for t in tiles(b)],
    }


# ---------------------------------------------------------------------------
# The run

@dataclass
class Run:
    today: dt.date
    now: dt.datetime               # ET, for "has today's last operation closed"
    old: dict
    keys: dict
    skip_seed: bool = False
    new: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)     # source -> {row label or '*': result text}
    reached: set = field(default_factory=set)       # sources read this run
    reread: set = field(default_factory=set)        # seeded sources whose values were compared this run
    newdata: set = field(default_factory=set)       # sources with a new data date this run
    fresh: dict = field(default_factory=dict)       # source -> newest data date read
    attention: list = field(default_factory=list)   # reasons to open the data issue
    unreached: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    layouts: list = field(default_factory=list)
    seed_mismatch: list = field(default_factory=list)
    cache: dict = field(default_factory=dict)       # parsed source data shared between steps
    nulls: list = field(default_factory=list)       # (source, field, date, why)
    pending: list = field(default_factory=list)     # (field, date): null placeholder, not published yet

    def __post_init__(self):
        self.new = copy.deepcopy(self.old)

    @property
    def D(self):
        return self.new["DATA"]

    def seeded(self, src: str) -> bool:
        rows = {r[0]: r for r in self.old["LOG"]}
        return any(rows[lab][5].startswith(SEED_MARK) or SEED_PENDING in rows[lab][5]
                   for lab, (p, _) in LOG_ROWS.items() if p == src and lab in rows)

    def result(self, src: str, text: str, rows: list[str] | None = None):
        """Result text for the source's log rows (or only `rows`)."""
        self.reached.add(src)
        d = self.results.setdefault(src, {})
        for lab in rows or [lab for lab, (p, o) in LOG_ROWS.items() if p == src or src in o]:
            d[lab] = text

    def not_reached(self, src: str, err):
        self.unreached.append(f"{SOURCE_NAMES[src]}: {err}")
        if isinstance(err, SourceError) and ("not found" in str(err) or "not parsed" in str(err)):
            self.layouts.append(f"{SOURCE_NAMES[src]}: {err}")
        self.results.setdefault(src, {})["*unreached*"] = f"Not updated: {SOURCE_NAMES[src]} not reached."

    def seed_compare(self, src: str, what: str, page, source):
        """Check 3: a seeded value re-read for the same date must equal the page."""
        self.reread.add(src)
        if page != source:
            self.seed_mismatch.append(f"{what}: page {dump(page)} vs source {dump(source)} ({SOURCE_NAMES[src]})")

    def null(self, src: str, what: str, date: str, why: str):
        self.nulls.append((src, what, date, why))
        self.attention.append(f"{SOURCE_NAMES[src]}: showed {date} but {why}; {what} set to null.")

    def not_published(self, fld: str, date: str):
        """A date the block holds because another series has it, while this series' newest print is
        older: the value is not published yet. A null placeholder, refilled by the next run's re-read;
        not a failed read, no issue."""
        self.pending.append((fld, date))

    def pending_text(self, fld: str, label: str) -> str:
        ds = [day_label(d) for f, d in self.pending if f == fld]
        return f"Not published yet: {label} for {', '.join(ds)}." if ds else ""

    def keep(self, src: str, date: str, why: str):
        self.attention.append(f"{SOURCE_NAMES[src]}: the re-read of {date} failed ({why}); the page value was kept.")


def new_text(date: str, how: str) -> str:
    return f"New {day_label(date) if len(date) == 10 else date}. {how}".strip()


def et_now() -> dt.datetime:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)


def prev_business_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(1)
    while not is_business_day(d):
        d -= dt.timedelta(1)
    return d


# ---------------------------------------------------------------------------
# H.4.1

def _h41_values(r: dict) -> dict:
    return {"t1": r["t1"], "assets": r["assets"]}


def _two_read(html: dict, xml: dict | None) -> list[str]:
    """Rows (and 'assets') where the HTML and the XML differ."""
    bad = [k for k in h41.T1_ROWS if html["t1"][k] != xml["t1"][k]]
    if html["assets"] != xml["assets"]:
        bad.append("assets")
    return bad


def _null_rows(v: dict, rows: list[str]) -> dict:
    v = copy.deepcopy(v)
    for k in rows:
        if k == "assets":
            v["assets"] = [None] * 3
        else:
            v["t1"][k] = [None] * 4
    return v


def do_h41(run: Run):
    old = run.old["DATA"]["h41"]
    try:
        cur = h41.get_html()
    except SourceError as e:
        return run.not_reached("h41", e)
    if cur["wed"] < old["wed"]:
        return run.not_reached("h41", f"newest week {cur['wed']} is older than the page's {old['wed']}")
    seeded = run.seeded("h41")
    is_new = cur["wed"] > old["wed"]
    if not is_new and not seeded:
        run.fresh["h41"] = cur["wed"]
        if _h41_values(cur) != _h41_values(old) or cur["release"] != old["release"]:
            run.keep("h41", cur["wed"], "the current release differs from the page for the same week")
        return run.result("h41", "No new release.")

    W = run.new["WEEKS"]
    since = (parse_iso(min(W["dates"] + [cur["wed"]])) - dt.timedelta(days=21)).isoformat()
    try:
        xml = h41.get_xml(since)
        xw = h41.xml_week(xml, cur["wed"])
        xml_note = None if xw else f"XML has no week {cur['wed']} yet"
    except SourceError as e:
        xml, xw, xml_note = None, None, f"XML not read: {e}"
        run.layouts.append(f"Fed H.4.1 XML: {e}") if "not found" in str(e) or " is " in str(e) else None
    run.reached.add("h41")
    run.fresh["h41"] = cur["wed"]
    run.cache["h41"] = cur

    if seeded:
        # Check 3: all WEEKS rows from their own dated releases, and the page's DATA.h41 week
        reads = {}
        for d in W["dates"]:
            try:
                reads[d] = cur if d == cur["wed"] else h41.get_html(h41.release_date_for(d))
            except SourceError as e:
                run.seed_mismatch.append(f"WEEKS {d}: dated release not read ({e})")
                continue
            r = reads[d]
            i = W["dates"].index(d)
            src_row = {"res": r["t1"]["reserves"][0], "tga": r["t1"]["tga"][0], "assets": r["assets"][0],
                       "rrp": r["t1"]["rrpOthers"][3]}
            for k, v in src_row.items():
                run.seed_compare("h41", f"WEEKS.{k} {d}", W[k][i], v)
                W[k][i] = v
        pr = reads.get(old["wed"])
        if pr is None and old["wed"] not in W["dates"]:
            try:
                pr = h41.get_html(h41.release_date_for(old["wed"]))
            except SourceError as e:
                run.seed_mismatch.append(f"DATA.h41 {old['wed']}: dated release not read ({e})")
        if pr is not None:
            run.seed_compare("h41", f"DATA.h41.release {old['wed']}", old["release"], pr["release"])
            for k in h41.T1_ROWS:
                run.seed_compare("h41", f"DATA.h41.t1.{k} {old['wed']}", old["t1"][k], pr["t1"][k])
            run.seed_compare("h41", f"DATA.h41.assets {old['wed']}", old["assets"], pr["assets"])
        if xml:
            xd = []
            for d in W["dates"]:
                w = h41.xml_week(xml, d)
                if w and d in reads and (w["t1"] != reads[d]["t1"] or w["assets"] != reads[d]["assets"]):
                    xd.append(d)
            run.notes.append(f"H.4.1 XML vs the 17 dated releases: {'all rows agree' if not xd else 'differ for ' + ', '.join(xd)} "
                             "(the XML carries revisions).")

    # Two reads (check 1) for the newest week
    vals = _h41_values(cur)
    bad_rows, why = [], {}
    if xw:
        for k in _two_read(vals, xw):
            bad_rows.append(k)
            why[k] = "H.4.1 HTML and XML differ"
        how = "H.4.1 HTML and XML agree." if not bad_rows else "H.4.1 HTML and XML differ (issue opened)."
    else:
        how = f"Single source ({xml_note})."
        run.notes.append(f"H.4.1 {cur['wed']}: single source ({xml_note}).")

    # Ties (check 2): week-average change, reverse repo total
    prev_vals = None
    if old["wed"] == cur["prev"]:
        prev_vals = _h41_values(old)
    elif xml and h41.xml_week(xml, cur["prev"]):
        prev_vals = h41.xml_week(xml, cur["prev"])
    elif cur["wed"] == old["wed"]:
        try:
            prev_vals = _h41_values(h41.get_html(h41.release_date_for(cur["prev"])))
        except SourceError as e:
            run.notes.append(f"H.4.1 week-average tie not run: week {cur['prev']} not read ({e}).")
    if prev_vals:
        xprev = h41.xml_week(xml, cur["prev"]) if xml else None
        for k in h41.T1_ROWS:
            a, p = vals["t1"][k], prev_vals["t1"][k]
            if abs(a[0] - p[0] - a[1]) <= 1:
                continue
            if xprev and abs(a[0] - xprev["t1"][k][0] - a[1]) <= 1:
                run.notes.append(f"H.4.1 {k}: week {cur['prev']} was revised ({p[0]} → {xprev['t1'][k][0]}); the change ties to the revised value.")
                continue
            bad_rows.append(k)
            why[k] = f"the {k} week-average change does not tie to the prior week"
        pa = prev_vals["assets"][0]
        if isnum(pa) and abs(vals["assets"][0] - pa - vals["assets"][1]) > 1:
            xa = xprev["assets"][0] if xprev else None
            if not (isnum(xa) and abs(vals["assets"][0] - xa - vals["assets"][1]) <= 1):
                bad_rows.append("assets")
                why["assets"] = "the Table 5 Wednesday change does not tie"
    t = vals["t1"]
    if any(abs(t["rrp"][j] - t["rrpForeign"][j] - t["rrpOthers"][j]) > 1 for j in range(4)):
        for k in ("rrp", "rrpForeign", "rrpOthers"):
            bad_rows.append(k)
            why[k] = "reverse repo total does not equal foreign plus others"
    run.cache["h41_bad"] = (bad_rows, why, how, is_new)
    run.cache["h41_vals"] = vals


def finish_h41(run: Run):
    """After DTS and the operations are read: the cross-source ties, then write DATA.h41 and WEEKS."""
    if "h41_vals" not in run.cache:
        return
    cur, vals = run.cache["h41"], run.cache["h41_vals"]
    bad_rows, why, how, is_new = run.cache["h41_bad"]
    wed = cur["wed"]
    # TGA Wednesday level = DTS closing balance that Wednesday
    dmap = run.cache.get("dts_all") or {}
    if wed in dmap:
        if abs(vals["t1"]["tga"][3] - dmap[wed]["close"]) > 1:
            bad_rows.append("tga")
            why["tga"] = f"TGA failed the DTS tie ({vals['t1']['tga'][3]} vs {dmap[wed]['close']})"
        else:
            how += f" TGA Wednesday ${vals['t1']['tga'][3]:,}M ties to the DTS close."
    else:
        run.notes.append(f"TGA tie not run: no DTS closing balance for {wed} in this run.")
    # Reverse repo Others and repurchase agreements Wednesday level = NY Fed operations that Wednesday
    omap = run.cache.get("ops_all") or {}
    if wed in omap and omap[wed]["rrp"] is not None:
        o = omap[wed]
        if abs(vals["t1"]["rrpOthers"][3] - o["rrp"]) > 1:
            bad_rows.append("rrpOthers")
            why["rrpOthers"] = f"reverse repo Others failed the NY Fed operations tie ({vals['t1']['rrpOthers'][3]} vs {o['rrp']})"
        if o["repo_out"] is not None and abs(vals["t1"]["repo"][3] - o["repo_out"]) > 1:
            bad_rows.append("repo")
            why["repo"] = f"repurchase agreements failed the NY Fed operations tie ({vals['t1']['repo'][3]} vs {o['repo_out']})"
        if "rrpOthers" not in why and "repo" not in why:
            how += " Wednesday reverse repo and repo tie to NY Fed operations."
    else:
        run.notes.append(f"Operations tie not run: no NY Fed operations for {wed} in this run.")

    bad_rows = list(dict.fromkeys(bad_rows))
    D = run.D
    if is_new:
        v = _null_rows(vals, bad_rows)
        for k in bad_rows:
            run.null("h41", f"DATA.h41 {k}", wed, why[k])
        D["h41"] = {"release": cur["release"], "wed": wed, "t1": v["t1"], "assets": v["assets"]}
        W = run.new["WEEKS"]
        if wed in W["dates"]:
            raise CheckFailed(f"WEEKS already holds {wed}")
        add = {"res": v["t1"]["reserves"][0], "tga": v["t1"]["tga"][0], "assets": v["assets"][0], "rrp": v["t1"]["rrpOthers"][3]}
        W["dates"].append(wed)
        for k, x in add.items():
            W[k].append(x)
        for k in ("dates", "res", "tga", "assets", "rrp"):
            del W[k][:-N_WEEKS]
        run.newdata.add("h41")
        text = new_text(wed, how)
        if bad_rows:
            text = f"Null: H.4.1 showed {day_label(wed)} but " + "; ".join(why[k] for k in bad_rows) + "; issue opened."
        run.result("h41", text)
    else:
        # The seeded week re-read: a failed check keeps the page value
        if bad_rows:
            run.keep("h41", wed, "; ".join(why[k] for k in bad_rows))
            for k in bad_rows:
                if k == "assets":
                    vals["assets"] = run.old["DATA"]["h41"]["assets"]
                else:
                    vals["t1"][k] = run.old["DATA"]["h41"]["t1"][k]
        D["h41"] = {"release": cur["release"], "wed": wed, "t1": vals["t1"], "assets": vals["assets"]}
        run.result("h41", f"Re-read {day_label(wed)}: matches the seed (17 weeks from the dated releases). {how}".strip())


# ---------------------------------------------------------------------------
# Treasury DTS

def do_dts(run: Run):
    old = run.old["DATA"]["dts"]
    since = (run.today - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    try:
        m = dts.get(since)
    except SourceError as e:
        return run.not_reached("dts", e)
    run.cache["dts_all"] = m
    d = max(m)
    if d < old["date"]:
        return run.not_reached("dts", f"newest record {d} is older than the page's {old['date']}")
    run.fresh["dts"] = d
    if run.seeded("dts") and old["date"] in m:
        run.seed_compare("dts", f"DATA.dts.close {old['date']}", old["close"], m[old["date"]]["close"])
    if d > old["date"]:
        run.D["dts"] = {"date": d, "close": m[d]["close"]}
        run.newdata.add("dts")
        run.result("dts", new_text(d, "Single source."))
    else:
        if m[d]["close"] != old["close"] and not run.seeded("dts"):
            run.keep("dts", d, f"closing balance now {m[d]['close']}")
        run.result("dts", "Re-read: matches the seed. Single source." if run.seeded("dts") else "No new release.")


# ---------------------------------------------------------------------------
# NY Fed rates, the IORB file, operations, policy

def do_prates(run: Run):
    try:
        run.cache["iorb"] = prates.get_xml()
        run.reached.add("prates")
    except SourceError as e:
        run.not_reached("prates", e)


def iorb_on(run: Run, d: str):
    s = run.cache.get("iorb")
    if s is not None:
        return prates.in_force(s, d)
    return None


def do_rates(run: Run):
    a, b = (run.today - dt.timedelta(days=LOOKBACK_DAYS)).isoformat(), run.today.isoformat()
    try:
        sofr = nyfed.get_rates("SOFR", a, b)
        effr = nyfed.get_rates("EFFR", a, b)
    except SourceError as e:
        return run.not_reached("rates", e)
    run.cache["effr"] = effr
    R0 = run.old["RATES"]
    newest = max(set(sofr) | set(effr))
    if newest < R0["dates"][-1]:
        return run.not_reached("rates", f"newest print {newest} is older than the page's {R0['dates'][-1]}")
    off = sorted(d for d in set(sofr) | set(effr) if not is_business_day(parse_iso(d)))
    if off:
        run.notes.append(f"SOFR/EFFR prints on non-business days ignored: {', '.join(off)}")
    dates = sorted(d for d in set(sofr) | set(effr) if is_business_day(parse_iso(d)))[-N_DAYS:]
    old_iorb = dict(zip(R0["dates"], R0["iorb"]))
    iorbs = []
    for d in dates:
        v = iorb_on(run, d)
        if v is None:
            # File not reached: IORB stays in force unless the target range changed
            v = old_iorb.get(d)
            if v is None:
                rng = effr.get(d, {})
                same = (rng.get("lo"), rng.get("hi")) == (run.old["POLICY"]["lo"], run.old["POLICY"]["hi"])
                v = run.old["POLICY"]["iorb"] if same else None
                if v is None:
                    run.null("prates", "RATES.iorb", d, "the target range changed and IORB could not be read")
        iorbs.append(v)
    new = {"dates": dates, "sofr": [sofr[d]["rate"] if d in sofr else None for d in dates],
           "effr": [effr[d]["rate"] if d in effr else None for d in dates], "iorb": iorbs}
    for k, prints in (("sofr", sofr), ("effr", effr)):
        latest = max(prints) if prints else ""
        held = dict(zip(R0["dates"], R0[k]))
        for d in dates:
            if d in prints:
                continue
            if d > latest:
                if held.get(d) is not None:   # never replace a value the page already holds
                    new[k][dates.index(d)] = held[d]
                else:
                    run.not_published(f"RATES.{k}", d)
            else:
                run.notes.append(f"{d}: {k.upper()} has no print for this business day.")
        for i, d in enumerate(R0["dates"]):
            if R0[k][i] is None and d in dates and new[k][dates.index(d)] is not None:
                run.notes.append(f"{k.upper()} {d} filled: {new[k][dates.index(d)]} (was not published yet).")
    if run.seeded("rates") or run.seeded("prates"):
        for i, d in enumerate(R0["dates"]):
            if run.seeded("rates"):
                if d in sofr or d in effr:
                    run.seed_compare("rates", f"RATES.sofr {d}", R0["sofr"][i], sofr.get(d, {}).get("rate"))
                    run.seed_compare("rates", f"RATES.effr {d}", R0["effr"][i], effr.get(d, {}).get("rate"))
                else:
                    run.seed_mismatch.append(f"RATES {d}: not in the source response")
            if run.seeded("prates") and "iorb" in run.cache:
                run.seed_compare("prates", f"RATES.iorb {d}", R0["iorb"][i], iorb_on(run, d))
    # Revisions of dates the page already holds (SOFR can be revised the same day)
    for i, d in enumerate(R0["dates"]):
        if d in dates:
            j = dates.index(d)
            for k in ("sofr", "effr"):
                if R0[k][i] is not None and new[k][j] is not None and R0[k][i] != new[k][j] and not run.seeded("rates"):
                    run.notes.append(f"{k.upper()} {d} revised: {R0[k][i]} → {new[k][j]}")
    run.new["RATES"] = new
    run.fresh["rates"] = dates[-1]
    rev = [d for d in dates if (sofr.get(d, {}).get("rev") or effr.get(d, {}).get("rev"))]
    if rev:
        run.notes.append(f"Revision indicator set for {', '.join(rev)}.")
    if new != R0:
        run.newdata.add("rates")
    seed = "Re-read: matches the seed. " if run.seeded("rates") else ""
    for key, rows in (("sofr", ["SOFR minus IORB"]),
                      ("effr", ["Fed funds minus IORB", "Fed funds target range", "Real fed funds rate"])):
        n0, n1 = last_num(R0[key])[1], last_num(new[key])[1]
        d0, d1 = R0["dates"][n0] if n0 >= 0 else "", new["dates"][n1] if n1 >= 0 else ""
        pend = run.pending_text(f"RATES.{key}", key.upper())
        if d1 > d0:
            run.result("rates", " ".join(x for x in (seed + new_text(d1, "Single source."), pend) if x), rows=rows)
        else:
            run.result("rates", (seed + (pend or ("No new print." if not seed else ""))).strip(),
                       rows=[r for r in rows if r != "Real fed funds rate"])
    if "iorb" in run.cache:
        run.result("prates", "Re-read: matches the seed (Fed policy rates file)." if run.seeded("prates") else
                   ("IORB from the Fed policy rates file." if dates[-1] > R0["dates"][-1] else "No change."),
                   rows=["IORB"])


def do_ops(run: Run):
    a, b = (run.today - dt.timedelta(days=LOOKBACK_DAYS)).isoformat(), run.today.isoformat()
    try:
        days = nyfed.get_ops(a, b)
    except SourceError as e:
        return run.not_reached("ops", e)
    want = prev_business_day(run.today).isoformat()
    if not days or max(days) < want:
        return run.not_reached("ops", f"newest operation date {max(days) if days else None} is before {want}; response not trusted")
    done = lambda d: d < run.today.isoformat() or (run.now.hour, run.now.minute) >= OPS_DAY_DONE
    usable = sorted(d for d in days if done(d) and is_business_day(parse_iso(d)))
    run.cache["ops_all"] = {d: days[d] for d in usable}
    term = [o for d in usable for o in days[d]["term"]]
    if term:
        run.notes.append("Non-overnight operations (in the H.4.1 Wednesday repo tie while outstanding, "
                         "not in standing repo usage): " + "; ".join(term))
    other = [o for d in usable for o in days[d]["other"]]
    if other:
        run.notes.append("Operations not counted (not results): " + "; ".join(other))
    O0 = run.old["OPS"]
    dates = usable[-N_DAYS:]
    if dates[-1] < O0["dates"][-1]:
        return run.not_reached("ops", f"newest complete day {dates[-1]} is older than the page's {O0['dates'][-1]}")
    new = {"dates": dates, "srf": [days[d]["srf"] for d in dates], "rrp": [days[d]["rrp"] for d in dates]}
    for k, what in (("srf", "overnight repo"), ("rrp", "overnight reverse repo")):
        latest = max((d for d in usable if days[d][k] is not None), default="")
        held = dict(zip(O0["dates"], O0[k]))
        for d in dates:
            if days[d][k] is not None:
                continue
            if d > latest:
                if held.get(d) is not None:   # never replace a value the page already holds
                    new[k][dates.index(d)] = held[d]
                else:
                    run.not_published(f"OPS.{k}", d)
            elif d > O0["dates"][-1]:
                run.null("ops", f"OPS.{k}", d, f"no {what} result was published")
        for i, d in enumerate(O0["dates"]):
            if O0[k][i] is None and d in dates and new[k][dates.index(d)] is not None:
                run.notes.append(f"OPS.{k} {d} filled: {new[k][dates.index(d)]} (was not published yet).")
    if run.seeded("ops"):
        for i, d in enumerate(O0["dates"]):
            if d in dates:
                j = dates.index(d)
                run.seed_compare("ops", f"OPS.srf {d}", O0["srf"][i], new["srf"][j])
                run.seed_compare("ops", f"OPS.rrp {d}", O0["rrp"][i], new["rrp"][j])
    else:
        for i, d in enumerate(O0["dates"]):
            if d in dates:
                j = dates.index(d)
                if (O0["srf"][i], O0["rrp"][i]) != (new["srf"][j], new["rrp"][j]):
                    run.notes.append(f"Operations {d} changed: srf {O0['srf'][i]} → {new['srf'][j]}, rrp {O0['rrp'][i]} → {new['rrp'][j]}")
    run.new["OPS"] = new
    run.fresh["ops"] = dates[-1]
    run.cache["ops_last"] = days[dates[-1]]
    run.cache["ops_last_date"] = dates[-1]
    pend = " ".join(x for x in (run.pending_text("OPS.srf", "standing repo"),
                                run.pending_text("OPS.rrp", "reverse repo")) if x)
    if dates[-1] > O0["dates"][-1]:
        run.newdata.add("ops")
        run.result("ops", " ".join(x for x in (new_text(dates[-1], "Single source."), pend) if x))
    else:
        run.result("ops", " ".join(x for x in ("Re-read: matches the seed." if run.seeded("ops") else "No new release.", pend) if x))


def last_decision(meetings: list[list[str]] | None, before: str) -> str | None:
    days = [m[1] for m in meetings or [] if m[1] < before]
    return max(days) if days else None


def do_policy(run: Run, meetings: list[list[str]] | None):
    """lo/hi (newest EFFR record), iorb (policy rates file), srf/rrp (operation offering rates), move,
    the implementation-note second read when a rate changed, the corridor checks."""
    P0, P = run.old["POLICY"], run.new["POLICY"]
    effr = run.cache.get("effr")
    last_ops = run.cache.get("ops_last")
    asof = run.cache.get("ops_last_date")
    if run.seeded("rates") and effr:
        d0 = run.old["RATES"]["dates"][-1]
        if d0 in effr:
            run.seed_compare("rates", f"POLICY.lo/hi (EFFR {d0})", [P0["lo"], P0["hi"]], [effr[d0]["lo"], effr[d0]["hi"]])
    if run.seeded("ops") and run.cache.get("ops_all", {}).get(P0["asof"]):
        o = run.cache["ops_all"][P0["asof"]]
        run.seed_compare("ops", f"POLICY.srf/rrp ({P0['asof']})", [P0["srf"], P0["rrp"]], [o["srf_rate"], o["rrp_rate"]])
    if run.seeded("prates") and "iorb" in run.cache:
        run.seed_compare("prates", f"POLICY.iorb ({P0['asof']})", P0["iorb"], iorb_on(run, P0["asof"]))
    if run.seeded("rates") and effr is not None and P0.get("move"):
        md = parse_iso(P0["move"]["date"])
        try:
            hist = nyfed.get_rates("EFFR", (md - dt.timedelta(10)).isoformat(), (md + dt.timedelta(10)).isoformat())
            first = min((d for d, r in hist.items() if (r["lo"], r["hi"]) == (P0["lo"], P0["hi"])), default=None)
            before = max((d for d in hist if first and d < first), default=None)
            src_move = None
            if first and before:
                src_move = {"date": (parse_iso(first) - dt.timedelta(1)).isoformat(),
                            "bp": js_round((P0["hi"] - hist[before]["hi"]) * 100)}
            run.seed_compare("rates", "POLICY.move", P0["move"], src_move)
        except SourceError as e:
            run.seed_mismatch.append(f"POLICY.move: EFFR history not read ({e})")

    if effr:
        dn = max(effr)
        lo, hi = effr[dn]["lo"], effr[dn]["hi"]
        if (lo, hi) != (P0["lo"], P0["hi"]) and isnum(lo) and isnum(hi):
            first = min(d for d, r in effr.items() if (r["lo"], r["hi"]) == (lo, hi))
            P["move"] = {"date": (parse_iso(first) - dt.timedelta(1)).isoformat(), "bp": js_round((hi - P0["hi"]) * 100)}
        P["lo"], P["hi"] = lo, hi
    if last_ops is not None:
        # A rate whose operation is not published yet keeps the page's rate (rates are in force until changed)
        for k in ("srf", "rrp"):
            if last_ops[k + "_rate"] is not None:
                P[k] = last_ops[k + "_rate"]
        P["asof"] = asof
    if "iorb" in run.cache and asof:
        P["iorb"] = iorb_on(run, P["asof"])
    elif (P["lo"], P["hi"]) != (P0["lo"], P0["hi"]):
        P["iorb"] = None
        run.null("prates", "POLICY.iorb", P["asof"], "the target range changed and IORB could not be read")

    changed = [k for k in ("lo", "hi", "iorb", "srf", "rrp") if P[k] != P0[k] and P[k] is not None]
    if changed:
        dday = last_decision(meetings, (run.today + dt.timedelta(1)).isoformat())
        if dday:
            try:
                note = prates.get_note(parse_iso(dday))
                bad = [k for k in ("lo", "hi", "iorb", "srf", "rrp") if P[k] is not None and P[k] != note[k]]
                if bad:
                    for k in bad:
                        run.null("prates" if k == "iorb" else "rates" if k in ("lo", "hi") else "ops", f"POLICY.{k}", P["asof"],
                                 f"{k} {P[k]} differs from the implementation note of {dday} ({note[k]})")
                        P[k] = None
                else:
                    run.notes.append(f"Policy change {', '.join(changed)} agrees with the implementation note of {dday}.")
            except SourceError as e:
                run.notes.append(f"Implementation note for {dday} not read ({e}); policy change is single source.")
        else:
            run.notes.append("Policy change seen but no FOMC decision day known; single source.")
    # Corridor checks
    if isnum(P["iorb"]) and isnum(P["lo"]) and isnum(P["hi"]) and not (P["lo"] < P["iorb"] <= P["hi"]):
        run.null("prates", "POLICY.iorb", P["asof"], f"IORB {P['iorb']} is outside the range {P['lo']}–{P['hi']}")
        P["iorb"] = None
    if isnum(P["rrp"]) and isnum(P["lo"]) and P["rrp"] > P["lo"]:
        run.null("ops", "POLICY.rrp", P["asof"], f"reverse repo rate {P['rrp']} is above the bottom of the range")
        P["rrp"] = None
    if isnum(P["srf"]) and isnum(P["hi"]) and P["srf"] < P["hi"]:
        run.null("ops", "POLICY.srf", P["asof"], f"standing repo rate {P['srf']} is below the top of the range")
        P["srf"] = None
    if meetings is not None:
        P["fomc"] = [m for m in meetings if m[1] >= run.today.isoformat()][:2]
        if run.seeded("fomc"):
            run.seed_compare("fomc", "POLICY.fomc", P0["fomc"], [m for m in meetings if m[1] >= P0["asof"]][:2])
        run.result("fomc", "Re-read: matches the seed." if run.seeded("fomc") else
                   ("New meeting dates." if P["fomc"] != P0["fomc"] else "No change."))


def do_rmp(run: Run):
    P0, P = run.old["POLICY"], run.new["POLICY"]
    try:
        r = rmp.get()
    except SourceError as e:
        if "not parsed" in str(e):
            run.layouts.append(f"NY Fed purchase schedule: {e}")
            run.attention.append(f"NY Fed purchase schedule: {e}; the page value was kept.")
            return run.result("rmp", "Not parsed: schedule sentence changed; page value kept; issue opened.")
        return run.not_reached("rmp", e)
    run.fresh["rmp"] = r["from"]
    old = P0["rmp"]
    if (r["from"], r["to"]) == (old["from"], old["to"]):
        if run.seeded("rmp"):
            run.seed_compare("rmp", "POLICY.rmp", {k: old[k] for k in ("from", "to", "rmp", "reinv")}, r)
        elif (r["rmp"], r["reinv"]) != (old["rmp"], old["reinv"]):
            run.keep("rmp", r["from"], "the amounts for the current period changed")
            return run.result("rmp", "Kept page value: amounts for the current period changed; issue opened.")
        return run.result("rmp", "Re-read: matches the seed." if run.seeded("rmp") else "No new period.")
    if r["from"] < old["from"]:
        return run.not_reached("rmp", f"newest period {r['from']} is older than the page's {old['from']}")
    P["rmp"] = {**r, "announced": run.today.isoformat()}
    run.newdata.add("rmp")
    run.result("rmp", new_text(r["from"], "New period; announcement date is the date the script first saw it."))


def do_bea(run: Run):
    old = run.old["DATA"]["pce"]
    try:
        rows = bea.get_table(run.keys.get("BEA_API_KEY", ""), run.today)
        p = bea.pce(rows)
    except SourceError as e:
        return run.not_reached("bea", e)
    run.fresh["bea"] = p["month"]
    if run.seeded("bea") and old["month"] in p["all"]:
        run.seed_compare("bea", f"DATA.pce.v {old['month']}", old["v"], p["all"][old["month"]])
    if p["month"] > old["month"]:
        run.D["pce"] = {"month": p["month"], "v": p["v"]}
        run.newdata.add("bea")
        run.result("bea", new_text(p["month"], "Core PCE y/y from the BEA index levels."), rows=["Real fed funds rate"])
    elif p["month"] == old["month"]:
        run.result("bea", "Core PCE re-read: matches the seed." if run.seeded("bea") else "No new release.",
                   rows=["Real fed funds rate"])
    else:
        run.not_reached("bea", f"newest month {p['month']} is older than the page's {old['month']}")


# ---------------------------------------------------------------------------
# The check log

def log_values(b: dict) -> dict[str, tuple[str, str]]:
    D, W, P = b["DATA"], b["WEEKS"], b["POLICY"]
    H, t1 = D["h41"], D["h41"]["t1"]
    x = derived(b)
    wk = f"Wk ending {day_label(H['wed'])}"
    dl = lambda d: day_label(d) if d else "n/a"
    nl = (x["nl_now"] - x["nl13"]) if isnum(x["nl_now"]) and isnum(x["nl13"]) else None
    nl_b = js_round(nl / 1e3) * 1000 if isnum(nl) else None
    rmp_v = P["rmp"]["rmp"]
    to = dl(P["rmp"]["to"])
    nxt = P["fomc"][0] if P["fomc"] else None

    def fomc_label(m):
        if not m:
            return "n/a"
        a, z = dl(m[0]), dl(m[1])
        return f"{a}{EN_DASH}{z.split(' ')[1] if a.split(' ')[0] == z.split(' ')[0] else z}"

    real = js_round(x["real"] * 10) / 10 if isnum(x["real"]) else None
    return {
        "Bank reserves": (usd_t(t1["reserves"][0]), wk),
        "Treasury General Account": (usd_b(t1["tga"][0]), wk),
        "Treasury account, daily": (usd_b(D["dts"]["close"]), dl(D["dts"]["date"])),
        "Overnight reverse repo": (usd_b(x["onrrp"], 1), dl(x["rrp_date"])),
        "Fed total assets, y/y": (sgn_b(H["assets"][2]), dl(H["wed"])),
        "Net liquidity, 13-wk change": (sgn_b(nl_b), f"{dl(x['nl13_date'])} → {dl(W['dates'][-1])}"),
        "SOFR minus IORB": (bp_s(x["gap_s"]), dl(x["sofr_date"])),
        "Fed funds minus IORB": (bp_s(x["gap_e"]), dl(x["effr_date"])),
        "Standing repo facility usage": (usd_b(x["srf"], 3), dl(x["srf_date"])),
        "Fed loans to banks": (usd_b(t1["loans"][0], 1), wk),
        "Real fed funds rate": ((f"{'+' if real > 0 else MINUS if real < 0 else ''}{abs(real):.1f}%" if isnum(real) else "n/a"),
                                f"EFFR {dl(x['effr_date'])}; core PCE {['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][int(D['pce']['month'][5:]) - 1]}"),
        "Fed funds target range": (f"{P['lo']:.2f}{EN_DASH}{P['hi']:.2f}%" if isnum(P["lo"]) and isnum(P["hi"]) else "n/a",
                                   dl(x["effr_date"])),
        "IORB": (pct(P["iorb"]), dl(P["asof"])),
        "Standing repo rate": (pct(P["srf"]), dl(P["asof"])),
        "Overnight reverse repo rate": (pct(P["rrp"]), dl(P["asof"])),
        "Treasury bills held, y/y": (sgn_b(t1["bills"][2]), wk),
        "Mortgage bonds held, y/y": (sgn_b(t1["mbs"][2]), wk),
        "Reserve management purchases": (("None planned" if rmp_v == 0 else f"${rmp_v}B planned" if isnum(rmp_v) else "n/a") + f" to {to}",
                                         f"Announced {dl(P['rmp']['announced'])}"),
        "Next FOMC meeting": (fomc_label(nxt), f"{nxt[0][:4]} calendar" if nxt else "n/a"),
    }


def build_log(run: Run) -> list[list[str]]:
    """Rewrite only the rows a source touched this run (Checked = today). A seeded row whose own source was
    not re-read keeps its seed marker, so the seed check runs on a later run."""
    vals = log_values(run.new)
    checked = day_label(run.today.isoformat())
    out = []
    for row in run.old["LOG"]:
        lab = row[0]
        primary, others = LOG_ROWS[lab]
        texts = []
        for src in (primary, *others):
            r = run.results.get(src, {})
            t = r.get(lab) or (r.get("*unreached*") if src == primary else None)
            if t:
                texts.append((src, t.strip()))
        seeded_row = row[5].startswith(SEED_MARK) or SEED_PENDING in row[5]
        if seeded_row and primary not in run.reread:
            if not any(src != primary for src, _ in texts):
                out.append(row)
                continue
            texts = [(s, t) for s, t in texts if s != primary]
            texts.append((primary, f"{'not reached; ' if primary not in run.reached else ''}seed {SEED_PENDING}."))
        if not texts:
            out.append(row)
            continue
        if len({s for s, _ in texts}) > 1:
            result = " ".join(f"{SOURCE_NAMES[s]}: {t}" for s, t in texts)
        else:
            result = " ".join(dict.fromkeys(t for _, t in texts))
        v, d = vals[lab]
        out.append([lab, v, d, LOG_SOURCE_TEXT.get(lab, row[3]), checked, result])
    return out


# ---------------------------------------------------------------------------
# Diffs, moves, shapes

def flatten(b: dict) -> dict[str, tuple[str, dict]]:
    """{field: (source, {data date: value})} for every value the script writes (CAL and LOG aside)."""
    D, W, R, O, P = b["DATA"], b["WEEKS"], b["RATES"], b["OPS"], b["POLICY"]
    H = D["h41"]
    out = {}
    for k, v in H["t1"].items():
        for j, c in enumerate(("avg", "chg", "chgy", "wed")):
            out[f"DATA.h41.t1.{k}.{c}"] = ("h41", {H["wed"]: v[j]})
    for j, c in enumerate(("level", "chg", "chgy")):
        out[f"DATA.h41.assets.{c}"] = ("h41", {H["wed"]: H["assets"][j]})
    out["DATA.h41.release"] = ("h41", {H["wed"]: H["release"]})
    out["DATA.dts.close"] = ("dts", {D["dts"]["date"]: D["dts"]["close"]})
    out["DATA.pce.v"] = ("bea", {D["pce"]["month"]: D["pce"]["v"]})
    for k in ("res", "tga", "assets", "rrp"):
        out[f"WEEKS.{k}"] = ("h41", dict(zip(W["dates"], W[k])))
    out["WEEKS.netliq"] = ("h41", dict(zip(W["dates"], derived(b)["netliq"])))
    for k, src in (("sofr", "rates"), ("effr", "rates"), ("iorb", "prates")):
        out[f"RATES.{k}"] = (src, dict(zip(R["dates"], R[k])))
    for k in ("srf", "rrp"):
        out[f"OPS.{k}"] = ("ops", dict(zip(O["dates"], O[k])))
    for k, src in (("lo", "rates"), ("hi", "rates"), ("iorb", "prates"), ("srf", "ops"), ("rrp", "ops")):
        out[f"POLICY.{k}"] = (src, {P["asof"]: P[k]})
    out["POLICY.move"] = ("rates", {P["move"]["date"]: P["move"]["bp"]} if P.get("move") else {})
    out["POLICY.rmp"] = ("rmp", {P["rmp"]["from"]: dump(P["rmp"])})
    out["POLICY.fomc"] = ("fomc", {"meetings": dump(P["fomc"])})
    return out


RATE_FIELDS = {"RATES.sofr", "RATES.effr", "RATES.iorb", "POLICY.lo", "POLICY.hi", "POLICY.iorb", "POLICY.srf",
               "POLICY.rrp", "DATA.pce.v"}


@dataclass
class Diff:
    field: str
    source: str
    date: str
    old: object
    new: object
    old_date: str | None
    revision: bool
    added: bool = False


def diff_blocks(old: dict, new: dict, pending=()) -> list[Diff]:
    """Changed values. Not-published-yet placeholders (`pending`) are not changes."""
    fo, fn = flatten(old), flatten(new)
    pend = set(pending)
    out = []
    for f, (src, nv) in fn.items():
        ov = fo.get(f, (src, {}))[1]
        for d in sorted(nv):
            if d in ov and ov[d] == nv[d]:
                continue
            if nv[d] is None and (f, d) in pend:
                continue
            if d not in ov and len(nv) > 1 and d != max(nv):
                out.append(Diff(f, src, d, None, nv[d], None, False, added=True))
                continue
            od = d if d in ov else (max(ov) if ov else None)
            out.append(Diff(f, src, d, ov.get(d) if d in ov else (ov[od] if od else None), nv[d], od,
                            d in ov and ov[d] is not None))
    return out


def moves(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """Check 4: newest value vs the previous newest value. Returns (all moves, level-series moves)."""
    fo, fn = flatten(old), flatten(new)
    out, levels = [], []
    for f, (src, nv) in fn.items():
        if f.endswith((".chg", ".chgy")) or f in ("DATA.h41.release", "POLICY.move", "POLICY.rmp", "POLICY.fomc"):
            continue
        ov = fo.get(f, (src, {}))[1]
        if not nv or not ov or max(nv) == max(ov) and nv[max(nv)] == ov[max(ov)]:
            continue
        a, b = ov[max(ov)], nv[max(nv)]
        if not isnum(a) or not isnum(b) or a == b:
            continue
        if f in RATE_FIELDS:
            if abs(b - a) > RATE_MOVE + 1e-9:
                out.append(f"{f}: {a} → {b} ({(b - a) * 100:+.0f}bp)")
        elif abs(a) < SMALL_AMOUNT and abs(b) < SMALL_AMOUNT:
            continue
        elif a and abs(b / a - 1) * 100 > PCT_MOVE:
            line = f"{f}: {a} → {b} ({(b / a - 1) * 100:+.1f}%)"
            out.append(line)
            lim = LEVEL_MOVE_PCT.get(f, PCT_MOVE)
            if f in LEVEL_MOVE_FIELDS and abs(b / a - 1) * 100 > lim:
                levels.append(f"Level series moved more than {lim:.0f}% in a week: {line}")
    return out, levels


def check_shapes(b: dict) -> None:
    """Check 6: array lengths and order."""
    W, R, O = b["WEEKS"], b["RATES"], b["OPS"]
    if not all(len(W[k]) == N_WEEKS for k in ("dates", "res", "tga", "assets", "rrp")):
        raise CheckFailed(f"check 6: WEEKS arrays are not all {N_WEEKS} long")
    if any(parse_iso(d).weekday() != 2 for d in W["dates"]) or any(
            (parse_iso(b2) - parse_iso(a2)).days != 7 for a2, b2 in zip(W["dates"], W["dates"][1:])):
        raise CheckFailed("check 6: WEEKS dates are not consecutive Wednesdays")
    for name, blk, keys in (("RATES", R, ("dates", "sofr", "effr", "iorb")), ("OPS", O, ("dates", "srf", "rrp"))):
        if not all(len(blk[k]) == N_DAYS for k in keys):
            raise CheckFailed(f"check 6: {name} arrays are not all {N_DAYS} long")
        if any(b2 <= a2 for a2, b2 in zip(blk["dates"], blk["dates"][1:])):
            raise CheckFailed(f"check 6: {name} dates are not ascending without duplicates")
        if any(not is_business_day(parse_iso(d)) for d in blk["dates"]):
            raise CheckFailed(f"check 6: {name} holds a weekend or holiday")
    keys = [cal.sort_key(c) for c in b["CAL"]]
    if keys != sorted(keys):
        raise CheckFailed("check 6: CAL is not sorted by date and time")


# ---------------------------------------------------------------------------
# Alerts (GitHub issues, opened by scripts/alerts.py)

def alerts(run: Run, old_summary: dict | None) -> list[dict]:
    """[{kind: 'new'|'sticky', title, body, on}] from the new blocks."""
    out = []
    b, P0, P = run.new, run.old["POLICY"], run.new["POLICY"]
    t_new = tiles(b)
    prev = {t["name"]: t for t in (old_summary or {}).get("tiles", [])}
    for t in t_new:
        o = prev.get(t["name"])
        if o and o["status"] != "none" and t["status"] != "none" and o["status"] != t["status"]:
            out.append({"kind": "new", "title": f"Alert: {t['name']} turned {t['label']} ({t['asof']})",
                        "body": f"{t['name']}: {o['label']} → {t['label']}. Value {t['value']} (data date {t['asof']})."})
    changed = [k for k in ("lo", "hi", "iorb", "srf", "rrp") if isnum(P0[k]) and isnum(P[k]) and P0[k] != P[k]]
    r0, r1 = P0["rmp"], P["rmp"]
    rmp_changed = r1["from"] != r0["from"] and r1["rmp"] != r0["rmp"]
    if changed or rmp_changed:
        lines = [f"- {k}: {P0[k]} → {P[k]}" for k in changed]
        if rmp_changed:
            lines.append(f"- reserve management purchases: ${r0['rmp']}B ({r0['from']} to {r0['to']}) → ${r1['rmp']}B ({r1['from']} to {r1['to']})")
        out.append({"kind": "new", "title": f"Alert: Fed policy change ({P['asof'] if changed else r1['from']})",
                    "body": "\n".join(lines)})
    x, x0 = derived(b), derived(run.old)
    if "rates" in run.newdata:
        for name, key, d_key, iorb_key in (("SOFR", "sofr", "sofr_date", "sofr_iorb"), ("EFFR", "effr", "effr_date", "effr_iorb")):
            v, d, i = x[key], x[d_key], x[iorb_key]
            if isnum(v) and isnum(i) and d and (not x0[d_key] or d > x0[d_key]):
                out.append({"kind": "sticky", "title": f"Alert: {name} above IORB", "on": v > i,
                            "body": f"{name} {v:.2f}% on {d}, IORB {i:.2f}% ({bp_s(to_bp(v, i))})."})
    if "ops" in run.newdata and isnum(x["srf"]) and x["srf_date"] and (not x0["srf_date"] or x["srf_date"] > x0["srf_date"]):
        out.append({"kind": "sticky", "title": "Alert: Standing repo usage above $25B", "on": x["srf"] > SRF_ALERT,
                    "body": f"Standing repo usage ${x['srf']:,}M on {x['srf_date']}."})
    if "h41" in run.newdata:
        c = b["DATA"]["h41"]["t1"]["reserves"][1]
        if isnum(c) and abs(c) > RESERVES_ALERT:
            out.append({"kind": "new", "title": f"Alert: Reserves moved {sgn_b(c)} in week of {b['DATA']['h41']['wed']}",
                        "body": f"Reserve balances, week average {usd_t(b['DATA']['h41']['t1']['reserves'][0], 3)}, "
                                f"change from the prior week {sgn_b(c)} (Fed H.4.1 released {b['DATA']['h41']['release']})."})
    return out


# ---------------------------------------------------------------------------
# Running

class Report:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, line: str = ""):
        self.lines.append(line)
        print(line)

    def write(self):
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(self.lines) + "\n")


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def update(run: Run) -> None:
    """Every source; then the cross-source ties, the policy block, the calendar and the log."""
    meetings = pce_dates = None
    try:
        meetings = fomc.get(sorted({run.today.year, run.today.year + 1}))
        run.reached.add("fomc")
    except SourceError as e:
        run.not_reached("fomc", e)
    try:
        pce_dates = bea.get_dates()
    except SourceError as e:
        run.unreached.append(f"BEA release dates: {e}")
    do_h41(run)
    do_dts(run)
    do_prates(run)
    do_rates(run)
    do_ops(run)
    finish_h41(run)
    do_policy(run, meetings)
    do_rmp(run)
    do_bea(run)
    run.new["CAL"] = cal.build(run.today, run.new["DATA"]["h41"]["release"], meetings, pce_dates,
                               run.new["POLICY"]["rmp"]["to"], run.old["CAL"])
    run.new["LOG"] = build_log(run)


def commit_message(run: Run, all_moves: list[str]) -> str:
    names = {"h41": "H.4.1 week of", "dts": "DTS", "rates": "SOFR/EFFR", "ops": "operations", "bea": "core PCE",
             "rmp": "purchase period from"}
    fresh = [f"{names.get(s, s)} {d}" for s, d in run.fresh.items() if s in run.newdata]
    lines = [f"Data: {', '.join(fresh) if fresh else 'revisions and first reads'}", ""]
    lines.append("Data dates read: " + "; ".join(f"{SOURCE_NAMES[s]} {d}" for s, d in run.fresh.items()))
    if all_moves:
        lines += ["", f"Moves over {PCT_MOVE:.0f}% (or {RATE_MOVE * 100:.0f}bp for rates):"]
        lines += [f"- {m}" for m in all_moves]
    if run.unreached:
        lines += ["", "Not reached:"] + [f"- {u[:200]}" for u in run.unreached]
    return "\n".join(lines) + "\n"


def run_update(page: Path, summary_path: Path, dry_run: bool, commit_msg: Path | None = None,
               issue_body: Path | None = None, alerts_path: Path | None = None, today: dt.date | None = None,
               now: dt.datetime | None = None, keys: dict | None = None, skip_seed: bool = False,
               updater=update) -> int:
    say = Report()
    say(f"## Fed Liquidity Plumbing update{' (dry run)' if dry_run else ''}")
    set_output("changed", "false")
    set_output("attention", "false")
    set_output("alerts", "false")
    now = now or et_now()
    today = today or now.date()
    keys = keys if keys is not None else {"BEA_API_KEY": os.environ.get("BEA_API_KEY", "")}
    run = None
    try:
        html = page.read_text(encoding="utf-8")
        old = load_page(html)
        run = Run(today=today, now=now, old=old, keys=keys, skip_seed=skip_seed)
        say(f"- Run date (ET): {today}. Page data blocks last written {old['DATA']['updated']}.")
        updater(run)
        if run.seed_mismatch and not skip_seed:
            raise CheckFailed("check 3: seed values differ from the sources (nothing overwritten):\n"
                              + "\n".join(f"- {x}" for x in run.seed_mismatch))
        diffs = diff_blocks(old, run.new, run.pending)
        data_changed = bool(diffs)   # CAL and LOG ride along with a data change; alone they commit nothing
        if diffs:
            run.new["DATA"]["updated"] = today.isoformat()
        check_shapes(run.new)
        all_moves, level_moves = moves(old, run.new)
        new_html = render_page(html, run.new)
        verify_page(html, new_html, run.new)
    except CheckFailed as e:
        say(f"\n**Check failed, nothing written:** {e}")
        for title, items in (("Sources not reached", run and run.unreached), ("Values set to null this run (why)", run and run.attention),
                             ("Layouts to map", run and run.layouts), ("Notes", run and run.notes)):
            if items:
                say(f"\n### {title}")
                for u in items:
                    say(f"- {u}")
        if issue_body:
            issue_body.write_text(f"The data update failed a check and committed nothing.\n\n{e}\n", encoding="utf-8")
        say.write()
        return 1

    if level_moves:
        run.attention += level_moves
    old_summary = None
    if summary_path.exists():
        try:
            old_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except ValueError:
            run.notes.append("data/summary.json could not be read; color-change alerts skipped.")
    al = alerts(run, old_summary) if diffs else []

    say("\nAll checks passed.\n")
    say("### Values that change\n")
    if diffs:
        say("| Field | Old → new | Data date | Source |")
        say("|---|---|---|---|")
        for d in diffs:
            if d.added:
                change = f"added: {dump(d.new)}"
            else:
                od = f" ({d.old_date})" if d.old_date and d.old_date != d.date else ""
                change = f"{dump(d.old)}{od} → {dump(d.new)}{' (revised)' if d.revision else ''}"
            say(f"| `{d.field}` | {change} | {d.date} | {SOURCE_NAMES[d.source]} |")
    else:
        say("Nothing new from any source.")
    say(f"\nCalendar: {len(run.new['CAL'])} items{' (changed)' if run.new['CAL'] != old['CAL'] else ''}. "
        f"Log rows rewritten: {sum(1 for a, b in zip(old['LOG'], run.new['LOG']) if a != b)}.")
    if run.reread:
        say(f"\nSeed checks run for: {', '.join(SOURCE_NAMES[s] for s in sorted(run.reread))}"
            f"{' (differences accepted: skip_seed_checks)' if skip_seed and run.seed_mismatch else ' (all equal)'}.")
        if skip_seed and run.seed_mismatch:
            say("\n".join(f"- {m}" for m in run.seed_mismatch))
    say(f"\n### Moves over {PCT_MOVE:.0f}% (or {RATE_MOVE * 100:.0f}bp for rates; amounts under $5B exempt)\n")
    say("\n".join(f"- {m}" for m in all_moves) if all_moves else "None.")
    say(f"\n### Alerts{' that would open' if dry_run else ''}\n")
    if al:
        for a in al:
            state = "" if a["kind"] == "new" else (" (open or comment)" if a["on"] else " (close if open)")
            say(f"- {a['title']}{state}: {a['body']}")
    else:
        say("None.")
    if run.pending:
        say("\n### Not published yet (null placeholders; the next run re-reads and fills them)\n")
        say("\n".join(f"- `{f}` {d}" for f, d in run.pending))
    say("\n### Sources not reached\n")
    say("\n".join(f"- {u}" for u in run.unreached) if run.unreached else "None.")
    if run.layouts:
        say("\n### Layouts to map (a parser could not find its fields)\n")
        say("\n".join(f"- {l}" for l in run.layouts))
    if run.notes:
        say("\n### Notes\n")
        say("\n".join(f"- {n}" for n in run.notes))
    if run.attention:
        say(f"\n**Needs attention ({'an issue would be opened' if dry_run else 'an issue will be opened'}):**")
        for a in run.attention:
            say(f"- {a}")
    if skip_seed:
        say("\nSeed checks were skipped for this run (workflow input).")

    if not data_changed:
        say("\nNothing new: no commit.")
    elif dry_run:
        say("\nDry run: nothing written.")
    else:
        page.write_text(new_html, encoding="utf-8")
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(build_summary(run.new), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if commit_msg:
            commit_msg.write_text(commit_message(run, all_moves), encoding="utf-8")
        set_output("changed", "true")
        if al and alerts_path:
            alerts_path.write_text(json.dumps(al, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            set_output("alerts", "true")
    if run.attention and not dry_run:
        if issue_body:
            issue_body.write_text("\n\n".join(run.attention) + "\n", encoding="utf-8")
        set_output("attention", "true")
    say.write()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch, parse and check; write nothing")
    ap.add_argument("--page", type=Path, default=ROOT / "index.html")
    ap.add_argument("--summary", type=Path, default=ROOT / "data" / "summary.json")
    ap.add_argument("--commit-msg", type=Path, help="where to write the commit message")
    ap.add_argument("--issue-body", type=Path, help="where to write the text for a 'needs attention' issue")
    ap.add_argument("--alerts", type=Path, help="where to write the alert issues for scripts/alerts.py")
    ap.add_argument("--skip-seed-checks", action="store_true",
                    help="accept source values that differ from the seeded ones (after reviewing them)")
    ap.add_argument("--summary-only", action="store_true",
                    help="rewrite data/summary.json from the page and exit (no fetching)")
    a = ap.parse_args(argv)
    if a.summary_only:
        blocks = load_page(a.page.read_text(encoding="utf-8"))
        a.summary.parent.mkdir(parents=True, exist_ok=True)
        a.summary.write_text(json.dumps(build_summary(blocks), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    return run_update(a.page, a.summary, a.dry_run, a.commit_msg, a.issue_body, a.alerts, skip_seed=a.skip_seed_checks)


if __name__ == "__main__":
    sys.exit(main())
