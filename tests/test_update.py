"""The update run against the frozen seed page and the fixtures: seed checks, the new-week path, failed
ties, the page checks, the tile rules (parity with the page's own status() in Node), alerts."""
import copy
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from conftest import NOW, ROOT, TODAY, dated_reads, seed_html

import alerts as alerts_mod
import update
from sources.common import CheckFailed


def seed():
    return update.load_page(seed_html())


def write_page(tmp_path: Path, blocks: dict | None = None) -> Path:
    html = seed_html() if blocks is None else update.render_page(seed_html(), blocks)
    p = tmp_path / "index.html"
    p.write_text(html, encoding="utf-8")
    return p


def run(tmp_path, page, dry_run=False, skip_seed=False):
    return update.run_update(page, tmp_path / "data" / "summary.json", dry_run, commit_msg=tmp_path / "msg.txt",
                             issue_body=tmp_path / "issue.md", alerts_path=tmp_path / "alerts.json", today=TODAY,
                             now=NOW, keys={"BEA_API_KEY": "x"}, skip_seed=skip_seed)


def unseeded(b: dict) -> dict:
    """The page as after its first successful run: no log row carries a seed marker."""
    b = copy.deepcopy(b)
    for r in b["LOG"]:
        if r[5].startswith(update.SEED_MARK) or update.SEED_PENDING in r[5]:
            r[5] = "Checked."
    return b


def week_earlier() -> dict:
    """The seed page one H.4.1 week back (Oct 7 in DATA.h41; WEEKS Jun 17..Oct 7), not seeded."""
    b = unseeded(seed())
    r = dated_reads()["2026-10-07"]
    b["DATA"]["h41"] = {"release": r["release"], "wed": r["wed"], "t1": r["t1"], "assets": r["assets"]}
    W = b["WEEKS"]
    b["WEEKS"] = {"dates": ["2026-06-17"] + W["dates"][:-1], **{k: [W[k][0]] + W[k][:-1] for k in ("res", "tga", "assets", "rrp")}}
    return b


# ---------------------------------------------------------------------------
# The page

def test_block_round_trip():
    html = seed_html()
    assert update.render_page(html, update.load_page(html)) == html


def test_nine_blocks_and_one_line_each():
    b = seed()
    assert list(b) == list(update.BLOCK_NAMES)


def test_verify_page_rejects_other_lines_and_fixed_blocks():
    html = seed_html()
    b = update.load_page(html)
    update.verify_page(html, html, b)
    with pytest.raises(CheckFailed, match="check 5"):
        update.verify_page(html, html.replace("For education only.", "For education only!"), b)
    lines = html.splitlines(keepends=True)
    i = update.block_lines(lines)["NOTES"]
    lines[i] = lines[i].replace("third month", "3rd month")
    with pytest.raises(CheckFailed, match="check 5"):
        update.verify_page(html, "".join(lines), b)


def test_node_reads_blocks_back():
    html = seed_html()
    update.node_check(html, update.load_page(html))


def test_css_before_additions_is_the_yield_monitors():
    """The CSS before the 'Fed Liquidity Plumbing additions' comment was compared with the Yield Regime
    Monitor's page on Oct 20, 2026 (identical); its hash is frozen here."""
    css = re.search(r"<style>(.*?)</style>", seed_html(), re.S).group(1)
    pre = css.split("/* ---- Fed Liquidity Plumbing additions")[0].rstrip()
    assert hashlib.sha256(pre.encode("utf-8")).hexdigest() == \
        "eef5af08feb0fca43b3a6c58d0075e538216617d3039686d2b4d4124b081025e"


def test_disclaimer_directly_under_header():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    m = re.search(r"</header>\s*<p class=\"disclaimer\"><b>For education only\. Not financial advice\.</b>", html)
    assert m


def test_index_html_still_the_seed_outside_data_lines():
    """index.html in the repo may carry newer data lines, never other changes."""
    update.verify_page(seed_html(), (ROOT / "index.html").read_text(encoding="utf-8"),
                       update.load_page((ROOT / "index.html").read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Runs

def test_seed_run_rereads_everything_and_changes_nothing(offline, tmp_path):
    page = write_page(tmp_path)
    assert run(tmp_path, page) == 0
    assert page.read_text(encoding="utf-8") == seed_html()
    assert not (tmp_path / "data" / "summary.json").exists()


def test_seed_run_report(offline, tmp_path, capsys):
    page = write_page(tmp_path)
    assert run(tmp_path, page, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "All checks passed." in out
    assert "(all equal)" in out
    for s in ("Fed H.4.1", "NY Fed SOFR/EFFR", "NY Fed operation results", "Treasury DTS", "Fed policy rates file",
              "BEA", "NY Fed purchase schedule", "Fed FOMC calendar"):
        assert s in out.split("Seed checks run for:")[1].split("\n")[0]
    assert "Nothing new from any source." in out


def test_seed_mismatch_stops_the_run(offline, tmp_path, capsys):
    b = seed()
    b["WEEKS"]["res"][3] += 1
    page = write_page(tmp_path, b)
    before = page.read_text(encoding="utf-8")
    assert run(tmp_path, page) == 1
    out = capsys.readouterr().out
    assert "check 3" in out and "WEEKS.res 2026-07-15: page 3142722 vs source 3142721" in out
    assert page.read_text(encoding="utf-8") == before
    assert "check 3" in (tmp_path / "issue.md").read_text(encoding="utf-8")


def test_seed_mismatch_accepted_with_skip(offline, tmp_path):
    b = seed()
    b["WEEKS"]["res"][3] += 1
    page = write_page(tmp_path, b)
    assert run(tmp_path, page, skip_seed=True) == 0
    assert update.load_page(page.read_text(encoding="utf-8"))["WEEKS"]["res"][3] == 3142721


def test_seed_check_covers_policy(offline, tmp_path, capsys):
    b = seed()
    b["POLICY"]["move"]["bp"] = 50
    page = write_page(tmp_path, b)
    assert run(tmp_path, page, dry_run=True) == 1
    assert "POLICY.move" in capsys.readouterr().out


def test_new_week(offline, tmp_path):
    page = write_page(tmp_path, week_earlier())
    assert run(tmp_path, page) == 0
    b, S = update.load_page(page.read_text(encoding="utf-8")), seed()
    assert b["DATA"]["h41"] == S["DATA"]["h41"]
    assert b["WEEKS"] == S["WEEKS"]
    assert b["DATA"]["updated"] == "2026-10-20"
    row = {r[0]: r for r in b["LOG"]}["Bank reserves"]
    assert row[1:5] == ["$3.02T", "Wk ending Oct 14", "Fed H.4.1", "Oct 20"]
    assert row[5].startswith("New Oct 14. H.4.1 HTML and XML agree.")
    assert "ties to the DTS close" in row[5] and "tie to NY Fed operations" in row[5]
    assert b["NOTES"] == S["NOTES"] and b["AUDIT"] == S["AUDIT"]
    summary = json.loads((tmp_path / "data" / "summary.json").read_text(encoding="utf-8"))
    assert summary["h41_week"] == "2026-10-14" and summary["netliq_13w_B"] == -115
    assert "H.4.1 week of 2026-10-14" in (tmp_path / "msg.txt").read_text(encoding="utf-8")
    assert not (tmp_path / "issue.md").exists()


def test_new_week_tga_fails_dts_tie(offline, tmp_path):
    real = offline["dts"]

    def bad(since):
        m = real(since)
        m["2026-10-14"]["close"] += 50
        return m
    offline["dts"] = bad
    page = write_page(tmp_path, week_earlier())
    assert run(tmp_path, page) == 0
    b = update.load_page(page.read_text(encoding="utf-8"))
    assert b["DATA"]["h41"]["t1"]["tga"] == [None] * 4
    assert b["WEEKS"]["tga"][-1] is None and b["WEEKS"]["dates"][-1] == "2026-10-14"
    assert b["DATA"]["h41"]["t1"]["reserves"] == seed()["DATA"]["h41"]["t1"]["reserves"]
    row = {r[0]: r for r in b["LOG"]}["Treasury General Account"]
    assert row[1] == "n/a"
    assert row[5].startswith("Null: H.4.1 showed Oct 14 but TGA failed the DTS tie")
    assert "set to null" in (tmp_path / "issue.md").read_text(encoding="utf-8")


def test_new_week_html_and_xml_differ(offline, tmp_path):
    real = offline["h41_xml"]

    def bad(since):
        x = real(since)
        x["2026-10-14"]["bills"][0] += 5
        return x
    offline["h41_xml"] = bad
    page = write_page(tmp_path, week_earlier())
    assert run(tmp_path, page) == 0
    b = update.load_page(page.read_text(encoding="utf-8"))
    assert b["DATA"]["h41"]["t1"]["bills"] == [None] * 4
    assert "HTML and XML differ" in (tmp_path / "issue.md").read_text(encoding="utf-8")


def test_new_week_xml_missing_is_single_source(offline, tmp_path):
    from sources.common import SourceError

    def gone(since):
        raise SourceError("FRB_h41_xml.zip: HTTP 503")
    offline["h41_xml"] = gone
    page = write_page(tmp_path, week_earlier())
    assert run(tmp_path, page) == 0
    b = update.load_page(page.read_text(encoding="utf-8"))
    assert b["DATA"]["h41"] == seed()["DATA"]["h41"]
    assert "Single source (XML not read" in {r[0]: r for r in b["LOG"]}["Bank reserves"][5]


def test_source_not_reached_keeps_values(offline, tmp_path):
    from sources.common import SourceError

    def gone(*a):
        raise SourceError("HTTP 503")
    for k in ("h41_current", "dts", "iorb", "rates", "ops", "rmp", "bea", "fomc", "bea_dates"):
        offline[k] = gone
    page = write_page(tmp_path)
    assert run(tmp_path, page) == 0
    assert page.read_text(encoding="utf-8") == seed_html()


def test_new_rates_day(offline, tmp_path):
    real = offline["rates"]

    def more(kind, a, b):
        r = real(kind, a, b)
        r["2026-10-19"] = {"rate": 3.92 if kind == "SOFR" else 3.88, "rev": "", **({"lo": 3.75, "hi": 4.0} if kind == "EFFR" else {})}
        return r
    offline["rates"] = more
    page = write_page(tmp_path, unseeded(seed()))
    assert run(tmp_path, page) == 0
    b = update.load_page(page.read_text(encoding="utf-8"))
    assert b["RATES"]["dates"][-1] == "2026-10-19" and b["RATES"]["dates"][0] == "2026-10-05"
    assert (b["RATES"]["sofr"][-1], b["RATES"]["iorb"][-1]) == (3.92, 3.9)
    al = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    sofr = [a for a in al if a["title"] == "Alert: SOFR above IORB"]
    assert sofr and sofr[0]["on"] is True and "+2bp" in sofr[0]["body"]
    assert [a for a in al if a["title"] == "Alert: EFFR above IORB"][0]["on"] is False
    row = {r[0]: r for r in b["LOG"]}["SOFR minus IORB"]
    assert row[1:3] == ["+2bp", "Oct 19"] and row[5] == "New Oct 19. Single source."


def test_dry_run_writes_nothing_and_lists_alerts(offline, tmp_path, capsys):
    page = write_page(tmp_path, week_earlier())
    before = page.read_text(encoding="utf-8")
    assert run(tmp_path, page, dry_run=True) == 0
    out = capsys.readouterr().out
    assert page.read_text(encoding="utf-8") == before
    assert not (tmp_path / "alerts.json").exists() and not (tmp_path / "msg.txt").exists()
    assert "### Alerts that would open" in out
    assert "| `WEEKS.res` | added" not in out and "`WEEKS.res`" in out


def test_tests_do_not_see_github_step_files():
    assert "GITHUB_STEP_SUMMARY" not in os.environ and "GITHUB_OUTPUT" not in os.environ


# ---------------------------------------------------------------------------
# Checks

def test_shapes():
    b = seed()
    update.check_shapes(b)
    c = copy.deepcopy(b)
    c["WEEKS"]["res"].pop()
    with pytest.raises(CheckFailed, match="WEEKS"):
        update.check_shapes(c)
    c = copy.deepcopy(b)
    c["RATES"]["dates"][0] = "2026-10-03"   # a Saturday
    with pytest.raises(CheckFailed, match="RATES"):
        update.check_shapes(c)
    c = copy.deepcopy(b)
    c["CAL"] = c["CAL"][::-1]
    with pytest.raises(CheckFailed, match="CAL"):
        update.check_shapes(c)


def test_moves():
    a, b = seed(), seed()
    b["WEEKS"]["dates"] = b["WEEKS"]["dates"][1:] + ["2026-10-21"]
    for k, v in (("res", 2700000), ("tga", 900000), ("assets", 6760000), ("rrp", 4000)):
        b["WEEKS"][k] = b["WEEKS"][k][1:] + [v]
    b["RATES"]["sofr"][-1] = 4.05
    allm, levels = update.moves(a, b)
    assert any("WEEKS.res:" in m and "more than 5%" in m for m in levels)   # -10.7% in a week
    assert not any(m.startswith("WEEKS.rrp") for m in allm)         # under $5B: exempt
    assert any(m.startswith("RATES.sofr") and "+17bp" in m for m in allm)


def test_log_values_reproduce_the_seed():
    b = seed()
    vals = update.log_values(b)
    for r in b["LOG"]:
        assert list(vals[r[0]]) == r[1:3], r[0]


# ---------------------------------------------------------------------------
# Tiles: Python rules vs the page's own status() in Node

def node_tiles(blocks: dict) -> list:
    html = update.render_page(seed_html(), blocks)
    script = update.extract_script(html)
    js = script[:script.index("// ---------- Header pills")]
    js += "\nprocess.stdout.write(JSON.stringify(IND.map(i => [i.name, i.st, i.lab2])));\n"
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def variants():
    yield "seed", seed()
    b = seed()
    b["DATA"]["h41"]["t1"]["reserves"][0] = 2800000
    b["DATA"]["h41"]["t1"]["tga"][0] = 1050000
    b["OPS"]["srf"][-1] = 30000
    b["OPS"]["rrp"][-1] = 60000
    yield "stressed", b
    b = seed()
    b["RATES"]["effr"] = [None] * 10
    b["DATA"]["h41"]["assets"][2] = -250000
    b["RATES"]["sofr"][-1] = 3.95
    yield "nulls", b
    b = seed()
    b["RATES"]["sofr"][-1] = 3.9
    b["RATES"]["effr"][-1] = 3.94
    b["DATA"]["pce"]["v"] = 2.3
    b["DATA"]["h41"]["t1"]["loans"][0] = 31000
    b["WEEKS"]["tga"][-1] = None
    yield "edges", b


@pytest.mark.parametrize("name,blocks", list(variants()))
def test_tile_status_parity_with_page(name, blocks):
    py = [[t["name"], t["status"], t["label"]] for t in update.tiles(blocks)]
    assert py == node_tiles(blocks)


def test_summary_json_matches_seed():
    committed = json.loads((ROOT / "data" / "summary.json").read_text(encoding="utf-8"))
    assert committed == update.build_summary(update.load_page((ROOT / "index.html").read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Alerts

def make_run(new: dict, newdata=()):
    r = update.Run(today=TODAY, now=NOW, old=seed(), keys={})
    r.new = new
    r.newdata = set(newdata)
    return r


def test_alert_color_change_and_policy_change():
    old_summary = update.build_summary(seed())
    b = seed()
    b["DATA"]["h41"]["t1"]["reserves"][0] = 2950000
    b["POLICY"]["iorb"] = 3.65
    b["POLICY"]["asof"] = "2026-10-29"
    al = update.alerts(make_run(b), old_summary)
    titles = [a["title"] for a in al]
    assert "Alert: Bank reserves turned Tightening (2026-10-14)" in titles
    assert "Alert: Fed policy change (2026-10-29)" in titles
    assert "iorb: 3.9 → 3.65" in [a for a in al if a["title"].startswith("Alert: Fed policy")][0]["body"]


def test_alert_unknown_tile_is_not_a_color_change():
    old_summary = update.build_summary(seed())
    b = seed()
    b["DATA"]["h41"]["t1"]["reserves"][0] = None
    assert update.alerts(make_run(b), old_summary) == []


def test_alert_rmp_restart_and_reserves_move():
    b = seed()
    b["POLICY"]["rmp"] = {"from": "2026-11-14", "to": "2026-12-11", "rmp": 10, "reinv": 15.0, "announced": "2026-11-13"}
    b["DATA"]["h41"]["t1"]["reserves"][1] = -120000
    titles = [a["title"] for a in update.alerts(make_run(b, ["h41", "rmp"]), None)]
    assert "Alert: Fed policy change (2026-11-14)" in titles
    assert "Alert: Reserves moved −$120B in week of 2026-10-14" in titles


def test_alert_standing_repo():
    b = seed()
    b["OPS"]["dates"] = b["OPS"]["dates"][1:] + ["2026-10-20"]
    b["OPS"]["srf"] = b["OPS"]["srf"][1:] + [26000]
    b["OPS"]["rrp"] = b["OPS"]["rrp"][1:] + [300]
    al = update.alerts(make_run(b, ["ops"]), None)
    assert [(a["title"], a["on"]) for a in al] == [("Alert: Standing repo usage above $25B", True)]


class FakeGitHub:
    def __init__(self, issues):
        self.issues, self.calls = issues, []

    def ensure_label(self):
        self.calls.append(("label",))

    def open_issues(self):
        return self.issues

    def create(self, title, body):
        self.calls.append(("create", title))
        return {"number": 99}

    def assign(self, n):
        self.calls.append(("assign", n))

    def comment(self, n, body):
        self.calls.append(("comment", n))

    def close(self, n):
        self.calls.append(("close", n))


def test_alerts_apply():
    gh = FakeGitHub([{"number": 7, "title": "Alert: SOFR above IORB"}, {"number": 8, "title": "Alert: EFFR above IORB"}])
    al = [{"kind": "new", "title": "Alert: Fed policy change (2026-10-29)", "body": "x"},
          {"kind": "sticky", "title": "Alert: SOFR above IORB", "on": True, "body": "y"},
          {"kind": "sticky", "title": "Alert: EFFR above IORB", "on": False, "body": "z"},
          {"kind": "sticky", "title": "Alert: Standing repo usage above $25B", "on": True, "body": "w"},
          {"kind": "sticky", "title": "Alert: Standing repo usage above $25B", "on": False, "body": "v"}]
    done = alerts_mod.apply(al, gh)
    assert gh.calls == [("label",), ("create", "Alert: Fed policy change (2026-10-29)"), ("assign", 7), ("comment", 7),
                        ("comment", 8), ("close", 8), ("create", "Alert: Standing repo usage above $25B"),
                        ("comment", 99), ("close", 99)]
    assert len(done) == 5


# ---------------------------------------------------------------------------
# Public repo: no personal information in tracked files

def test_no_emails_or_session_links_in_repo():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    files += [str(p.relative_to(ROOT)) for p in (ROOT / "tests").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}")
    bad = []
    for f in sorted(set(files)):
        p = ROOT / f
        if not p.is_file() or p.suffix in (".png", ".zip"):
            continue
        t = p.read_text(encoding="utf-8", errors="ignore")
        if email.search(t) or ("claude" + ".ai/") in t:
            bad.append(f)
    assert bad == []


class FakeSession:
    def __init__(self):
        self.headers, self.sent = {}, []

    def request(self, method, url, **kw):
        self.sent.append((method, url.split("/repos/o/r")[1], kw.get("json")))

        class R:
            status_code, content = 200, b"{}"

            def json(self):
                return {"number": 5}
        return R()


def test_alert_issues_assigned_to_owner():
    s = FakeSession()
    gh = alerts_mod.GitHub("o/r", "t", "owner-from-env", session=s)
    gh.create("Alert: x", "b")
    gh.assign(3)
    assert s.sent[0] == ("POST", "/issues", {"title": "Alert: x", "body": "b", "labels": ["alert"], "assignees": ["owner-from-env"]})
    assert s.sent[1] == ("POST", "/issues/3/assignees", {"assignees": ["owner-from-env"]})


# ---------------------------------------------------------------------------
# Not published yet: SOFR posts about 8:00 am ET, EFFR about 9:00 am ET

def sofr_with_oct19(offline):
    """Run #1's state (8:40 am ET, Oct 20): SOFR has Oct 19, EFFR still ends Oct 16."""
    from conftest import text
    from sources import nyfed
    real = offline["rates"]
    sofr = nyfed.parse_rates(text("nyfed_sofr_search_20260928_20261020_with_oct19.json"), "SOFR")

    def rates(kind, a, b):
        if kind == "SOFR":
            return {d: v for d, v in sofr.items() if a <= d <= b}
        return real(kind, a, b)
    offline["rates"] = rates
    return real


def test_effr_not_published_yet_is_a_placeholder(offline, tmp_path, capsys):
    from conftest import text
    from sources import nyfed
    assert max(nyfed.parse_rates(text("nyfed_effr_search_20260928_20261020.json"), "EFFR")) == "2026-10-16"
    sofr_with_oct19(offline)
    page = write_page(tmp_path, unseeded(seed()))
    assert run(tmp_path, page) == 0
    out = capsys.readouterr().out
    b = update.load_page(page.read_text(encoding="utf-8"))
    assert b["RATES"]["dates"][-1] == "2026-10-19"
    assert (b["RATES"]["sofr"][-1], b["RATES"]["effr"][-1]) == (3.87, None)
    assert "`RATES.effr`" not in out.split("### Values that change")[1].split("###")[0]   # not listed as a change
    assert "- `RATES.effr` 2026-10-19" in out.split("### Not published yet")[1]
    assert not (tmp_path / "issue.md").exists()                                         # no attention, no issue
    assert "Needs attention" not in out
    rows = {r[0]: r for r in b["LOG"]}
    assert rows["Fed funds minus IORB"][1:3] == ["−2bp", "Oct 16"]
    assert rows["Fed funds minus IORB"][5] == "Not published yet: EFFR for Oct 19."
    assert rows["SOFR minus IORB"][5] == "New Oct 19. Single source."
    assert b["POLICY"]["lo"] == 3.75 and b["POLICY"]["hi"] == 4.0                       # range from Oct 16, kept

    # The next run re-reads both series and fills the placeholder
    real = offline["rates"]

    def with_effr(kind, a, b):
        r = real(kind, a, b)
        if kind == "EFFR" and a <= "2026-10-19" <= b:
            r["2026-10-19"] = {"rate": 3.88, "rev": "", "lo": 3.75, "hi": 4.0}
        return r
    offline["rates"] = with_effr
    assert run(tmp_path, page) == 0
    out = capsys.readouterr().out
    b = update.load_page(page.read_text(encoding="utf-8"))
    assert b["RATES"]["effr"][-1] == 3.88
    assert "| `RATES.effr` | null → 3.88 | 2026-10-19 |" in out
    assert "EFFR 2026-10-19 filled: 3.88 (was not published yet)." in out
    assert "### Not published yet" not in out
    assert {r[0]: r for r in b["LOG"]}["Fed funds minus IORB"][5] == "New Oct 19. Single source."


def test_effr_not_published_yet_on_the_seeded_page(offline, tmp_path, capsys):
    """Dry run #1 exactly: the seeded page at 8:40 am ET."""
    sofr_with_oct19(offline)
    page = write_page(tmp_path)
    assert run(tmp_path, page, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "(all equal)" in out and "Needs attention" not in out
    changes = out.split("### Values that change")[1].split("###")[0]
    assert "→ null" not in changes and "`RATES.sofr`" in changes


def test_sofr_not_published_yet_either_way(offline, tmp_path):
    real = offline["rates"]

    def rates(kind, a, b):
        r = real(kind, a, b)
        if kind == "EFFR" and a <= "2026-10-19" <= b:
            r["2026-10-19"] = {"rate": 3.88, "rev": "", "lo": 3.75, "hi": 4.0}
        return r
    offline["rates"] = rates
    page = write_page(tmp_path, unseeded(seed()))
    assert run(tmp_path, page) == 0
    b = update.load_page(page.read_text(encoding="utf-8"))
    assert (b["RATES"]["sofr"][-1], b["RATES"]["effr"][-1]) == (None, 3.88)
    assert {r[0]: r for r in b["LOG"]}["SOFR minus IORB"][5] == "Not published yet: SOFR for Oct 19."
    assert not (tmp_path / "issue.md").exists()


def test_rates_gap_inside_the_series_is_only_a_note(offline, tmp_path, capsys):
    """A missing print older than the series' newest date is not 'not published yet'."""
    real = offline["rates"]
    offline["rates"] = lambda kind, a, b: {d: v for d, v in real(kind, a, b).items()
                                           if not (kind == "SOFR" and d == "2026-10-14")}
    page = write_page(tmp_path, unseeded(seed()))
    assert run(tmp_path, page, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "2026-10-14: SOFR has no print for this business day." in out
    assert "### Not published yet" not in out


@pytest.mark.parametrize("tga, listed, issue", [(952002, True, False), (1020000, True, True)])
def test_tga_level_move_limit_is_15_percent(tga, listed, issue):
    """Week of Oct 21: TGA 883,373 -> 952,002 (+7.8%) is a move in the summary, not an issue; over 15% is."""
    a, b = seed(), seed()
    b["WEEKS"]["dates"] = b["WEEKS"]["dates"][1:] + ["2026-10-21"]
    for k in ("res", "tga", "assets", "rrp"):
        b["WEEKS"][k] = b["WEEKS"][k][1:] + [tga if k == "tga" else b["WEEKS"][k][-1]]
    allm, levels = update.moves(a, b)
    assert any(m.startswith("WEEKS.tga:") for m in allm) == listed
    assert [m for m in levels if "WEEKS.tga:" in m] == (
        [f"Level series moved more than 15% in a week: WEEKS.tga: 883373 → {tga} ({(tga / 883373 - 1) * 100:+.1f}%)"]
        if issue else [])


def test_week_of_oct_21_term_repo_ties(offline, tmp_path, capsys):
    """The Oct 22 run: H.4.1 repurchase agreements Wednesday 67 = the $67M term repo RP 102026 29 outstanding on
    Oct 21 (overnight repo 0). The row is written, not nulled, and the TGA's +7.8% week opens no issue."""
    from conftest import raw, text
    from sources import dts, h41, nyfed
    offline["h41_current"] = lambda: h41.parse_html(raw("h41_20261022.html"))
    real_dts, real_ops = offline["dts"], offline["ops"]
    offline["dts"] = lambda since: {**real_dts(since), **dts.parse(text("dts_operating_cash_balance_20261019_20261021.json"))}
    new_ops = nyfed.daily(nyfed.parse_ops(text("nyfed_rp_search_20261019_20261022.json")))
    offline["ops"] = lambda a, b: {**real_ops(a, b), **{d: v for d, v in new_ops.items() if a <= d <= b}}
    page = write_page(tmp_path, unseeded(seed()))
    import datetime as dt
    rc = update.run_update(page, tmp_path / "data" / "summary.json", False, commit_msg=tmp_path / "msg.txt",
                           issue_body=tmp_path / "issue.md", alerts_path=tmp_path / "alerts.json",
                           today=dt.date(2026, 10, 22), now=dt.datetime(2026, 10, 22, 18, 15), keys={"BEA_API_KEY": "x"})
    out = capsys.readouterr().out
    assert rc == 0
    b = update.load_page(page.read_text(encoding="utf-8"))
    D = b["DATA"]["h41"]
    assert (D["release"], D["wed"]) == ("2026-10-22", "2026-10-21")
    assert D["t1"]["repo"] == [27, 26, -1881, 67]
    assert b["WEEKS"]["dates"][-1] == "2026-10-21" and b["WEEKS"]["tga"][-1] == 952002
    assert b["OPS"]["dates"][-3:] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert b["OPS"]["srf"][-3:] == [0, 0, 1]                     # standing repo usage stays overnight-only
    row = {r[0]: r for r in b["LOG"]}["Bank reserves"][5]
    assert row.startswith("New Oct 21.") and "Wednesday reverse repo and repo tie to NY Fed operations." in row
    assert "ties to the DTS close" in row
    assert "Non-overnight operations" in out and "RP 102026 29 Repo Term (2 days) $67M" in out
    assert "WEEKS.tga: 883373 → 952002 (+7.8%)" in out           # listed as a move ...
    assert not (tmp_path / "issue.md").exists()                  # ... but no issue

def test_ops_not_published_yet(offline, tmp_path, capsys):
    """The newest day's reverse repo result is not out yet: a placeholder, the page's ON RRP rate kept."""
    real = offline["ops"]

    def ops(a, b):
        d = real(a, b)
        if "2026-10-19" in d:
            d["2026-10-19"] = {**d["2026-10-19"], "rrp": None, "rrp_rate": None}
        return d
    offline["ops"] = ops
    b0 = unseeded(seed())                      # the page one day back: OPS Oct 2 .. Oct 16
    O = b0["OPS"]
    b0["OPS"] = {"dates": ["2026-10-02"] + O["dates"][:-1], "srf": [0] + O["srf"][:-1], "rrp": [1501] + O["rrp"][:-1]}
    page = write_page(tmp_path, b0)
    assert run(tmp_path, page) == 0
    out = capsys.readouterr().out
    b = update.load_page(page.read_text(encoding="utf-8"))
    assert (b["OPS"]["dates"][-1], b["OPS"]["srf"][-1], b["OPS"]["rrp"][-1]) == ("2026-10-19", 11, None)
    assert "- `OPS.rrp` 2026-10-19" in out and "Needs attention" not in out
    assert not (tmp_path / "issue.md").exists()
    assert b["POLICY"]["rrp"] == 3.75
    rows = {r[0]: r for r in b["LOG"]}
    assert rows["Overnight reverse repo"][1:3] == ["$0.1B", "Oct 16"]
    assert rows["Overnight reverse repo"][5] == "New Oct 19. Single source. Not published yet: reverse repo for Oct 19."


def test_placeholder_never_replaces_a_held_value(offline, tmp_path):
    real = offline["ops"]

    def ops(a, b):
        d = real(a, b)
        d["2026-10-19"] = {**d["2026-10-19"], "rrp": None, "rrp_rate": None}
        return d
    offline["ops"] = ops
    page = write_page(tmp_path, unseeded(seed()))
    assert run(tmp_path, page, dry_run=True) == 0
    r = update.Run(today=TODAY, now=NOW, old=unseeded(seed()), keys={})
    update.do_ops(r)
    assert r.new["OPS"]["rrp"][-1] == 330 and r.pending == []
