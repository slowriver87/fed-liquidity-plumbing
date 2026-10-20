"""Shared fixtures: every source getter patched to read tests/fixtures instead of the network."""
import copy
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
FIX = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 10, 20)
NOW = dt.datetime(2026, 10, 20, 10, 45)   # ET, before today's 13:30 operation closes

from sources import bea, dts, fomc, h41, nyfed, prates, rmp  # noqa: E402
from sources.common import NotFound, SourceError  # noqa: E402


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def dated_reads() -> dict:
    return json.loads(text("h41_dated_reads_20260624_20261014.json"))["weeks"]


def seed_html() -> str:
    return text("seed_index.html")


@pytest.fixture(autouse=True)
def no_github_step_files(monkeypatch):
    """GitHub sets GITHUB_STEP_SUMMARY and GITHUB_OUTPUT for every step, the test step included. Without
    this, test runs of update.run_update would append fixture reports to the real run's Summary page."""
    for k in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)


def _between(d: dict, a: str, b: str) -> dict:
    return {k: v for k, v in d.items() if a <= k <= b}


@pytest.fixture
def offline(monkeypatch):
    """Patch every getter to its fixture. Returns a dict a test can edit to change one source."""
    rates = {k: {} for k in ("SOFR", "EFFR")}
    for name in ("nyfed_sofr_search_20260928_20261020.json",):
        rates["SOFR"].update(nyfed.parse_rates(text(name), "SOFR"))
    for name in ("nyfed_effr_search_20260928_20261020.json", "nyfed_effr_search_20260910_20260922.json"):
        rates["EFFR"].update(nyfed.parse_rates(text(name), "EFFR"))
    reads = dated_reads()
    src = {
        "h41_current": lambda: h41.parse_html(raw("h41_20261015.html")),
        "h41_dated": lambda wed: copy.deepcopy(reads[wed]) if wed in reads else (_ for _ in ()).throw(NotFound(wed)),
        "h41_xml": lambda since: h41.parse_xml(str(FIX / "h41_xml_excerpt.xml"), since),
        "dts": lambda since: dts.parse(text("dts_operating_cash_balance_20260928_20261016.json")),
        "iorb": lambda: prates.parse_xml(str(FIX / "prates_xml_excerpt.xml")),
        "note": lambda d: prates.parse_note(text(f"fed_implementation_note_{d:%Y%m%d}.txt")),
        "rates": lambda kind, a, b: _between(copy.deepcopy(rates[kind]), a, b),
        "ops": lambda a, b: _between(nyfed.daily(nyfed.parse_ops(text("nyfed_rp_search_20261001_20261020.json"))), a, b),
        "rmp": lambda: rmp.parse(text("nyfed_treasury_operational_details_table.html")),
        "bea": lambda key, today: bea.parse_api(text("bea_api_doc_sample_T20804.json")),
        "bea_dates": lambda: bea.parse_dates(text("bea_release_dates.json")),
        "fomc": lambda years: fomc.parse(text("fomc_2026_2027_panels.html"), years),
    }
    call = lambda k, *a: src[k](*a)

    def get_html(release=None):
        if release is None:
            return call("h41_current")
        return call("h41_dated", (release - dt.timedelta(1)).isoformat())

    def get_note(d):
        try:
            return call("note", d)
        except FileNotFoundError:
            raise SourceError(f"implementation note {d}: 404")

    monkeypatch.setattr(h41, "get_html", get_html)
    monkeypatch.setattr(h41, "get_xml", lambda since: call("h41_xml", since))
    monkeypatch.setattr(dts, "get", lambda since: call("dts", since))
    monkeypatch.setattr(prates, "get_xml", lambda: call("iorb"))
    monkeypatch.setattr(prates, "get_note", get_note)
    monkeypatch.setattr(nyfed, "get_rates", lambda kind, a, b: call("rates", kind, a, b))
    monkeypatch.setattr(nyfed, "get_ops", lambda a, b: call("ops", a, b))
    monkeypatch.setattr(rmp, "get", lambda: call("rmp"))
    monkeypatch.setattr(bea, "get_table", lambda key, today: call("bea", key, today))
    monkeypatch.setattr(bea, "get_dates", lambda: call("bea_dates"))
    monkeypatch.setattr(fomc, "get", lambda years: call("fomc", years))
    return src
