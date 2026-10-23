"""New York Fed Markets API: SOFR, EFFR, and repo / reverse repo operation results.

All three are read through the documented date-range search routes (tests/fixtures/nyfed_markets_api_routes.txt):
a `last/{N}` call once came back missing the two newest days. Layouts printed Oct 20, 2026:
- rates: {"refRates": [{effectiveDate, type, percentRate, ..., revisionIndicator}]}, newest first,
  numbers as JSON numbers; EFFR records also carry targetRateFrom / targetRateTo.
- operations: {"repo": {"operations": [{operationDate, operationType ("Repo" / "Reverse Repo"),
  operationMethod, term ("Overnight"), releaseTime, auctionStatus ("Results"), totalAmtAccepted (dollars),
  details: [{securityType, amtAccepted, percentOfferingRate, percentAwardRate (reverse repo)}]}]}}.
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
        for k in ("operationDate", "operationType", "term", "totalAmtAccepted"):
            if k not in o:
                raise SourceError(f"repo operations: field {k!r} missing in {o.get('operationId')}")
        if o["operationType"] not in ("Repo", "Reverse Repo"):
            raise SourceError(f"repo operations: unexpected operationType {o['operationType']!r}")
    return ops


def daily(ops: list[dict]) -> dict[str, dict]:
    """{date: {srf, rrp, srf_rate, rrp_rate, n, other}}. srf = all overnight Repo operations' accepted amounts
    (morning plus afternoon), rrp = the overnight Reverse Repo amount, both in $ millions (rrp None when the
    day has no reverse repo result yet). Rates are the operations' offering rates (standing repo minimum bid,
    ON RRP offering rate). `other` lists operations that are not overnight results (not counted)."""
    out = {}
    for o in ops:
        d = out.setdefault(o["operationDate"], {"srf": 0, "rrp": None, "srf_rate": None, "rrp_rate": None,
                                                "n": 0, "other": [], "_srf_dollars": 0, "_rrp_dollars": None})
        if o.get("term") != "Overnight" or o.get("auctionStatus", "Results") != "Results":
            d["other"].append(_describe(o))
            continue
        amt = o["totalAmtAccepted"]
        if not _isnum(amt):
            raise SourceError(f"repo operations: {o.get('operationId')} totalAmtAccepted {amt!r}")
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
    for day, d in out.items():
        d["srf"] = to_millions(d.pop("_srf_dollars")) if d["srf_rate"] is not None else None
        r = d.pop("_rrp_dollars")
        d["rrp"] = to_millions(r) if r is not None else None
        d["repo_out"], d["repo_terms"] = repo_outstanding(ops, day)
    return out


def _describe(o: dict) -> str:
    amt = o.get("totalAmtAccepted")
    amt = f"${to_millions(amt):,}M" if _isnum(amt) else "amount n/a"
    return (f"{o.get('operationId')} {o['operationType']} {o.get('term')} ({o.get('termCalenderDays')} days) "
            f"{o.get('auctionStatus')}, {amt}, settles {o.get('settlementDate')}, matures {o.get('maturityDate')}")


def repo_outstanding(ops: list[dict], day: str) -> tuple:
    """Repo operations outstanding at the end of `day` (settlementDate <= day < maturityDate, any term),
    in $ millions, and descriptions of the non-overnight ones among them. This is what the H.4.1
    'Repurchase agreements' Wednesday level holds: a term operation agreed on Tuesday that settles
    Wednesday and matures Friday counts on Wednesday."""
    total, terms = 0, []
    for o in ops:
        if o["operationType"] != "Repo" or o.get("auctionStatus", "Results") != "Results":
            continue
        s, m = o.get("settlementDate"), o.get("maturityDate")
        if not s or not m:
            raise SourceError(f"repo operations: {o.get('operationId')} has no settlement or maturity date")
        if s <= day < m:
            total += o["totalAmtAccepted"]
            if o.get("term") != "Overnight":
                terms.append(_describe(o))
    return to_millions(total), terms


def get_ops(start: str, end: str) -> dict[str, dict]:
    return daily(parse_ops(fetch(URL_OPS.format(a=start, b=end)).decode("utf-8")))
