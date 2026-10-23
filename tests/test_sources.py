"""Parsers against the printed fixtures (never the network)."""
import datetime as dt
import json

import pytest
from conftest import FIX, dated_reads, raw, seed_html, text

import update
from sources import bea, calendar, dts, fomc, h41, nyfed, prates, rmp
from sources.common import SourceError


def seed():
    return update.load_page(seed_html())


# ---------------------------------------------------------------------------
# H.4.1

def test_h41_html_matches_seed_week():
    r = h41.parse_html(raw("h41_20261015.html"))
    D = seed()["DATA"]["h41"]
    assert (r["release"], r["wed"], r["prev"], r["yago"]) == ("2026-10-15", "2026-10-14", "2026-10-07", "2025-10-15")
    assert r["t1"] == D["t1"]
    assert r["assets"] == D["assets"]


def test_h41_sample_row_and_signs():
    r = h41.parse_html(raw("h41_20261015.html"))
    assert r["t1"]["reserves"] == [3022140, -7519, 3112, 3030854]
    assert r["t1"]["repo"] == [1, 0, -2392, 1]          # 'Repurchase agreements', not the 'Others' under it
    assert r["t1"]["rrpOthers"] == [287, -943, -3998, 201]  # 'Others' under reverse repurchase agreements


def test_h41_dated_reads_equal_seed_weeks():
    """The setup PR's seed check for WEEKS: 17 dated releases, read from the build environment."""
    reads, W = dated_reads(), seed()["WEEKS"]
    assert sorted(reads) == W["dates"]
    for i, d in enumerate(W["dates"]):
        r = reads[d]
        assert [r["t1"]["reserves"][0], r["t1"]["tga"][0], r["assets"][0], r["t1"]["rrpOthers"][3]] == \
               [W["res"][i], W["tga"][i], W["assets"][i], W["rrp"][i]], d


def test_h41_oct8_fixture_equals_record():
    assert h41.parse_html(raw("h41_20261008.html")) == dated_reads()["2026-10-07"]


def test_h41_weekly_ties_on_the_seed_weeks():
    reads = dated_reads()
    ws = sorted(reads)
    for a, b in zip(ws, ws[1:]):
        for k in h41.T1_ROWS:
            assert abs(reads[b]["t1"][k][0] - reads[a]["t1"][k][0] - reads[b]["t1"][k][1]) <= 1, (b, k)
        assert reads[b]["assets"][0] - reads[a]["assets"][0] == reads[b]["assets"][1]
    for w in ws:
        t = reads[w]["t1"]
        assert all(abs(t["rrp"][j] - t["rrpForeign"][j] - t["rrpOthers"][j]) <= 1 for j in range(4))


@pytest.mark.parametrize("s,v", [("565,492", 565492), ("+ 4,725", 4725), ("- 7,519", -7519), ("-26,334", -26334),
                                 ("0", 0), ("+   11,680", 11680), ("...", None), ("", None), ("(0)", None)])
def test_h41_numbers(s, v):
    assert h41._num(s) == v


def test_h41_footnote_spans_are_stripped():
    from bs4 import BeautifulSoup
    td = BeautifulSoup('<td><span>Bills</span><span style="font-size:5.33pt; vertical-align:super">2</span></td>',
                       "html.parser").td
    assert h41._label(td) == "Bills"
    td = BeautifulSoup("<td>Loans<sup>7</sup></td>", "html.parser").td
    assert h41._label(td) == "Loans"


def test_h41_section_layout_change_raises():
    html = text("h41_20261015.html").replace(">Foreign official and international accounts<", ">Foreign official accounts<")
    with pytest.raises(SourceError, match="rows under 'Reverse repurchase agreements'"):
        h41.parse_html(html)


def test_h41_release_dates():
    assert h41.release_date_for("2026-10-14") == dt.date(2026, 10, 15)
    assert h41.release_date_for("2026-11-25") == dt.date(2026, 11, 27)   # Thanksgiving Thursday


def test_h41_xml_matches_html_for_every_row():
    x = h41.parse_xml(str(FIX / "h41_xml_excerpt.xml"), "2026-06-24")
    reads = dated_reads()
    assert sorted(x) == sorted(reads)
    for d, r in reads.items():
        w = h41.xml_week(x, d)
        assert w["t1"] == r["t1"] and w["assets"] == r["assets"], d


def test_h41_xml_description_checked():
    s = text("h41_xml_excerpt.xml").replace("Reserve balances with Federal Reserve Banks: week average<",
                                            "Reserve balances with Federal Reserve Banks: weekly average<")
    with pytest.raises(SourceError, match="RESH4R_XAW_N.WW"):
        h41.parse_xml(s.encode("utf-8"))


# ---------------------------------------------------------------------------
# New York Fed

def test_rates_parse():
    s = nyfed.parse_rates(text("nyfed_sofr_last10.json"), "SOFR")
    e = nyfed.parse_rates(text("nyfed_effr_last10.json"), "EFFR")
    R = seed()["RATES"]
    assert [s[d]["rate"] for d in R["dates"]] == R["sofr"]
    assert [e[d]["rate"] for d in R["dates"]] == R["effr"]
    assert (e["2026-10-16"]["lo"], e["2026-10-16"]["hi"]) == (3.75, 4.0)
    with pytest.raises(SourceError):
        nyfed.parse_rates(text("nyfed_sofr_last10.json"), "EFFR")


def test_ops_daily_equals_seed():
    days = nyfed.daily(nyfed.parse_ops(text("nyfed_rp_search_20261001_20261020.json")))
    O = seed()["OPS"]
    assert [days[d]["srf"] for d in O["dates"]] == O["srf"]
    assert [days[d]["rrp"] for d in O["dates"]] == O["rrp"]
    assert (days["2026-10-19"]["srf_rate"], days["2026-10-19"]["rrp_rate"]) == (4.0, 3.75)


def test_ops_last_and_search_agree():
    a = nyfed.daily(nyfed.parse_ops(text("nyfed_rp_last12.json")))
    b = nyfed.daily(nyfed.parse_ops(text("nyfed_rp_search_20261001_20261020.json")))
    assert all(a[d] == b[d] for d in a)


def test_ops_wednesday_ties_to_h41():
    days = nyfed.daily(nyfed.parse_ops(text("nyfed_rp_wednesdays_20260624_20261014.json")))
    reads = dated_reads()
    for w, r in reads.items():
        assert days[w]["rrp"] == r["t1"]["rrpOthers"][3], w
        assert abs(days[w]["srf"] - r["t1"]["repo"][3]) <= 1, w
        assert days[w]["repo_out"] == days[w]["srf"], w      # no term repo outstanding on these Wednesdays
    assert days["2026-09-30"]["srf"] == 1200 and days["2026-09-30"]["rrp"] == 11539


def test_ops_units_and_other_terms():
    assert nyfed.to_millions(11_000_000) == 11
    assert nyfed.to_millions(2_340_000) == 2.3
    assert nyfed.to_millions(1_500_000) == 1.5
    ops = json.loads(text("nyfed_rp_last12.json"))["repo"]["operations"]
    ops[0] = {**ops[0], "term": "Term", "operationId": "RP X"}
    ops[1] = {**ops[1], "auctionStatus": "Announcement", "operationId": "RP Y"}
    d = nyfed.daily(ops)
    assert d[ops[0]["operationDate"]]["term"][0].startswith("RP X Repo Term (1 days)")
    assert d[ops[1]["operationDate"]]["other"] == [f"RP Y {ops[1]['operationType']} Overnight Announcement"]


def test_ops_term_repo_outstanding_oct_20_21():
    """RP 102026 29: a $67M two-day Small Value Exercise run Oct 20, settled Oct 21, maturing Oct 23. H.4.1
    repurchase agreements on Wednesday Oct 21 = 67 although every overnight repo that day was 0."""
    days = nyfed.daily(nyfed.parse_ops(text("nyfed_rp_search_20261019_20261022.json")))
    r = h41.parse_html(raw("h41_20261022.html"))
    assert r["wed"] == "2026-10-21" and r["t1"]["repo"] == [27, 26, -1881, 67]
    assert days["2026-10-21"]["srf"] == 0 and days["2026-10-21"]["repo_out"] == 67 == r["t1"]["repo"][3]
    assert days["2026-10-21"]["rrp"] == r["t1"]["rrpOthers"][3] == 237
    assert days["2026-10-20"]["srf"] == 0 and days["2026-10-20"]["repo_out"] == 0   # settles the next day
    assert days["2026-10-22"]["srf"] == 1 and days["2026-10-22"]["repo_out"] == 68  # still outstanding
    assert days["2026-10-20"]["term"] == ["RP 102026 29 Repo Term (2 days) $67M, settles 2026-10-21, matures 2026-10-23"]
    assert days["2026-10-20"]["n"] == 3 and days["2026-10-20"]["other"] == []


# ---------------------------------------------------------------------------
# Treasury DTS

def test_dts_close_in_open_today_bal():
    m = dts.parse(text("dts_operating_cash_balance_last10.json"))
    assert m["2026-10-16"]["close"] == seed()["DATA"]["dts"]["close"] == 948492
    r = dts.parse(text("dts_operating_cash_balance_20260928_20261016.json"))
    assert r["2026-10-14"]["close"] == r["2026-10-15"]["open"] == seed()["DATA"]["h41"]["t1"]["tga"][3]


# ---------------------------------------------------------------------------
# Policy rates

def test_prates_iorb():
    s = prates.parse_xml(str(FIX / "prates_xml_excerpt.xml"))
    assert prates.in_force(s, "2026-10-19") == 3.9
    assert prates.in_force(s, "2026-09-16") == 3.65 and prates.in_force(s, "2026-09-17") == 3.9
    R = seed()["RATES"]
    assert [prates.in_force(s, d) for d in R["dates"]] == R["iorb"]


def test_implementation_notes_both_forms():
    a = prates.parse_note(text("fed_implementation_note_20260916.txt"))
    assert a == {"statement": "2026-09-16", "effective": "2026-09-17", "iorb": 3.9, "lo": 3.75, "hi": 4.0,
                 "srf": 4.0, "rrp": 3.75}
    b = prates.parse_note(text("fed_implementation_note_20251029.txt"))   # 'minimum bid rate' wording
    assert (b["srf"], b["rrp"], b["lo"], b["hi"]) == (4.0, 3.75, 3.75, 4.0)
    with pytest.raises(SourceError, match="'srf'"):
        prates.parse_note(text("fed_implementation_note_20260916.txt").replace("at a rate of 4.0", "at 4.0"))


def test_note_fractions():
    assert prates._frac("3-3/4") == 3.75 and prates._frac("4") == 4.0 and prates._frac("1/4") == 0.25


# ---------------------------------------------------------------------------
# Purchase schedule, BEA, FOMC

def test_rmp_current_period():
    assert rmp.parse(text("nyfed_treasury_operational_details_table.html")) == \
           {"from": "2026-10-15", "to": "2026-11-13", "rmp": 0, "reinv": 14.6}
    P = seed()["POLICY"]["rmp"]
    assert (P["from"], P["to"], P["rmp"], P["reinv"]) == ("2026-10-15", "2026-11-13", 0, 14.6)


def test_rmp_sentences():
    s = ("The Desk plans to conduct approximately $16.5 billion in reinvestment purchases and an additional "
         "approximately $10 billion in reserve management purchases over the noted monthly period.")
    assert rmp.parse_sentence(s) == (16.5, 10)
    with pytest.raises(SourceError, match="not parsed"):
        rmp.parse_sentence("The Desk plans to purchase approximately $20 billion over the monthly period.")


def test_bea_core_pce_and_dates():
    p = bea.pce(bea.parse_api(text("bea_api_doc_sample_T20804.json")))
    assert (p["month"], p["v"]) == ("2026-08", seed()["DATA"]["pce"]["v"])
    d = [x for x in bea.parse_dates(text("bea_release_dates.json")) if x["d"] >= "2026-10-20"][:3]
    assert [(x["d"], x["hm"], x["ref"]) for x in d] == [("2026-10-29", (8, 30), "2026-09"),
                                                         ("2026-11-25", (8, 30), "2026-10"),
                                                         ("2026-12-23", (8, 30), "2026-11")]


def test_fomc_two_days():
    m = fomc.parse(text("fomc_2026_2027_panels.html"), [2026])
    assert m == [["2026-01-27", "2026-01-28"], ["2026-03-17", "2026-03-18"], ["2026-04-28", "2026-04-29"],
                 ["2026-06-16", "2026-06-17"], ["2026-07-28", "2026-07-29"], ["2026-09-15", "2026-09-16"],
                 ["2026-10-27", "2026-10-28"], ["2026-12-08", "2026-12-09"]]
    span = ('<div class="panel"><div class="panel-heading">2027 FOMC Meetings</div><div class="fomc-meeting">'
            '<div class="fomc-meeting__month">Apr/May</div><div class="fomc-meeting__date">30-1</div></div></div>')
    assert fomc.parse(span, [2027]) == [["2027-04-30", "2027-05-01"]]


# ---------------------------------------------------------------------------
# Calendar rules

def test_calendar_matches_rules_on_seed_date():
    S = seed()
    pce = bea.parse_dates(text("bea_release_dates.json"))
    meetings = fomc.parse(text("fomc_2026_2027_panels.html"), [2026, 2027])
    c = calendar.build(dt.date(2026, 10, 20), "2026-10-15", meetings, pce, "2026-11-13", S["CAL"])
    # Every seeded event is reproduced; the 28-day window adds the Nov 12 H.4.1
    assert [x for x in c if x["d"] != "2026-11-12"] == S["CAL"]
    assert [x["d"] for x in c if x["d"] == "2026-11-12"] == ["2026-11-12"]


def test_calendar_holiday_and_quarter_end():
    c = calendar.build(dt.date(2026, 11, 20), "2026-11-19", None, None, None, [])
    h = [x for x in c if x["id"] == "h41"]
    assert ("2026-11-27", "H.4.1 balance sheet, week of Nov 25") in [(x["d"], x["ev"]) for x in h]
    assert [x["d"] for x in c if x["id"] == "me"] == ["2026-11-30"]
    c = calendar.build(dt.date(2026, 12, 10), None, None, None, None, [])
    assert [(x["d"], x["id"]) for x in c if x["id"] in ("me", "qe")] == [("2026-12-31", "qe")]
