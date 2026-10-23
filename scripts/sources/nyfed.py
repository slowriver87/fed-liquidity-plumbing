"""New York Fed Markets API: SOFR, EFFR, and repo / reverse repo operation results.

All three are read through the documented date-range search routes (tests/fixtures/nyfed_markets_api_routes.txt):
a `last/{N}` call once came back missing the two newest days. Layouts printed Oct 20, 2026:
- rates: {"refRates": [{effectiveDate, type, percentRate, ..., revisionIndicator}]}, newest first,
  numbers as JSON numbers; EFFR records also carry targetRateFrom / targetRateTo.
- operations: {"repo": {"operations": [{operationDate, operationType ("Repo" / "Reverse Repo"),
  operationMethod, term ("Overnight"), releaseTime, auctionStatus ("Results"), totalAmtAccepted (dollars),
  details: [{securityType, amtAccepted, percentOfferingRate, percentAwardRate (reverse repo)}]}]}}.
  Term operations (fixture nyfed_rp_search_20261019_20261022.json, RP 102026 29, a "Small Value Exercise")
  carry term "Term", termCalenderDays 2, a settlementDate after the operationDate and no offering rates.
"""
from __future__ import annotations

import json

from .common import SourceError, fetch

BASE = "https://markets.newyorkfed.org/api"
URL_RATE = {"SOFR": BASE + "/rates/secured/sofr/search.json?startDate={a}&endDate={b}",
            "EFFR": BASE + "/rates/unsecured/effr/search.json?startDate={a}&endDate={b}"}
URL_OPS = BASE + "/rp/results/search.json?startDate={a}&endDate={b}"


def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_rates(text: str, kind: str) -> dict[str, dict]:
    """-> {date: {rate, lo, hi, rev}}; lo/hi only for EFFR. A record without a numeric rate is skipped."""
    try:
        recs = json.loads(text)["refRates"]
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError(f"{kind}: {e}")
    out = {}
    for r in recs:
        if r.get("type") != kind:
            raise SourceError(f"{kind}: unexpected record type {r.get('type')!r}")
        if not _isnum(r.get("percentRate")):
            continue
        d = r["effectiveDate"]
        if d in out:
            raise SourceError(f"{kind}: two records for {d}")
        rec = {"rate": float(r["percentRate"]), "rev": r.get("revisionIndicator") or ""}
        if kind == "EFFR":
            lo, hi = r.get("targetRateFrom"), r.get("targetRateTo")
            rec["lo"] = float(lo) if _isnum(lo) else None
            rec["hi"] = float(hi) if _isnum(hi) else None
        out[d] = rec
    return out


def get_rates(kind: str, start: str, end: str) -> dict[str, dict]:
    return parse_rates(fetch(URL_RATE[kind].format(a=start, b=end)).decode("utf-8"), kind)


def to_millions(dollars: int | float):
    """Dollars -> $ millions: an integer when whole, else 1 decimal."""
    m = dollars / 1e6
    return int(round(m)) if abs(m - round(m)) < 1e-9 else round(m, 1)


def parse_ops(text: str) -> list[dict]:
    try:
        ops = json.loads(text)["repo"]["operations"]
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError(f"repo operations: {e}")
    for o in ops:
        for k in ("operationDate", "operationType", "term", "settlementDate", "maturityDate", "totalAmtAccepted"):
            if k not in o:
                raise SourceError(f"repo operations: field {k!r} missing in {o.get('operationId')}")
        if o["operationType"] not in ("Repo", "Reverse Repo"):
            raise SourceError(f"repo operations: unexpected operationType {o['operationType']!r}")
    return ops


def daily(ops: list[dict]) -> dict[str, dict]:
    """{date: {srf, rrp, srf_rate, rrp_rate, n, repo_out, term, other}}. srf = all overnight Repo operations'
    accepted amounts (morning plus afternoon), rrp = the overnight Reverse Repo amount, both in $ millions (rrp
    None when the day has no reverse repo result yet). Rates are the operations' offering rates (standing repo
    minimum bid, ON RRP offering rate). repo_out = every Repo result, any term, outstanding at the end of that
    day (settlementDate <= date < maturityDate), in $ millions: what H.4.1 shows as repurchase agreements on a
    Wednesday; None when the day's overnight repo is not published yet. `term` lists the non-overnight results
    (counted in repo_out, not in srf), `other` the operations that are not results (not counted)."""
    out = {}
    for o in ops:
        d = out.setdefault(o["operationDate"], {"srf": 0, "rrp": None, "srf_rate": None, "rrp_rate": None,
                                                "n": 0, "repo_out": None, "term": [], "other": [],
                                                "_srf_dollars": 0, "_rrp_dollars": None})
        if o.get("auctionStatus", "Results") != "Results":
            d["other"].append(f"{o.get('operationId')} {o['operationType']} {o.get('term')} {o.get('auctionStatus')}")
            continue
        amt = o["totalAmtAccepted"]
        if not _isnum(amt):
            raise SourceError(f"repo operations: {o.get('operationId')} totalAmtAccepted {amt!r}")
        if o.get("term") != "Overnight":
            d["term"].append(f"{o.get('operationId')} {o['operationType']} {o.get('term')} "
                             f"({o.get('termCalenderDays')} days) ${to_millions(amt):,}M, "
                             f"settles {o['settlementDate']}, matures {o['maturityDate']}")
            continue
        rates = {x.get("percentOfferingRate") for x in o.get("details") or []}
        if len(rates) != 1 or not _isnum(next(iter(rates))):
            raise SourceError(f"repo operations: {o.get('operationId')} offering rates {sorted(map(str, rates))}")
        rate = float(next(iter(rates)))
        key = "srf" if o["operationType"] == "Repo" else "rrp"
        if d[key + "_rate"] is not None and d[key + "_rate"] != rate:
            raise SourceError(f"repo operations: {o['operationDate']} {o['operationType']} rates differ")
        d[key + "_rate"] = rate
        d["n"] += 1
        if key == "srf":
            d["_srf_dollars"] += amt
        else:
            d["_rrp_dollars"] = (d["_rrp_dollars"] or 0) + amt
    for d in out.values():
        d["srf"] = to_millions(d.pop("_srf_dollars")) if d["srf_rate"] is not None else None
        r = d.pop("_rrp_dollars")
        d["rrp"] = to_millions(r) if r is not None else None
    repos = [o for o in ops if o["operationType"] == "Repo" and o.get("auctionStatus", "Results") == "Results"]
    for day, d in out.items():
        if d["srf"] is not None:
            d["repo_out"] = to_millions(sum(o["totalAmtAccepted"] for o in repos
                                            if o["settlementDate"] <= day < o["maturityDate"]))
    return out


def get_ops(start: str, end: str) -> dict[str, dict]:
    return daily(parse_ops(fetch(URL_OPS.format(a=start, b=end)).decode("utf-8")))
