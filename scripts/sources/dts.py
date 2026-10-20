"""Treasury Fiscal Data: Daily Treasury Statement, operating cash balance (Table I).

Layout printed Oct 20, 2026: {data, meta, links}; values are strings in $ millions. The closing balance
sits in `open_today_bal` of the row whose account_type is "Treasury General Account (TGA) Closing
Balance"; its `close_today_bal` is the string "null".
"""
from __future__ import annotations

import json

from .common import SourceError, fetch

URL = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
       "?filter=record_date:gte:{a}&sort=-record_date&page%5Bsize%5D=200")
OPEN = "Treasury General Account (TGA) Opening Balance"
CLOSE = "Treasury General Account (TGA) Closing Balance"


def _int(s, where: str) -> int:
    try:
        return int(str(s).replace(",", ""))
    except ValueError:
        raise SourceError(f"DTS: {where} value {s!r} is not a number")


def parse(text: str) -> dict[str, dict]:
    """-> {record_date: {open, close}} ($ millions). A date with no closing-balance row is left out."""
    try:
        rows = json.loads(text)["data"]
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError(f"DTS: {e}")
    out: dict[str, dict] = {}
    for r in rows:
        at, d = r.get("account_type"), r.get("record_date")
        if at == CLOSE:
            if d in out and "close" in out[d]:
                raise SourceError(f"DTS: two closing balances for {d}")
            out.setdefault(d, {})["close"] = _int(r.get("open_today_bal"), f"{d} closing balance")
        elif at == OPEN:
            out.setdefault(d, {})["open"] = _int(r.get("open_today_bal"), f"{d} opening balance")
    if not out:
        raise SourceError(f"DTS: no {CLOSE!r} rows")
    return {d: v for d, v in out.items() if "close" in v}


def get(since: str) -> dict[str, dict]:
    return parse(fetch(URL.format(a=since)).decode("utf-8"))
