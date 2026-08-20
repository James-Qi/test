#!/usr/bin/env python3
"""One-shot bounded data fetch for US ETF R0 research.

Outputs only 2026-07-01..2026-08-19 OHLCV for frozen U2 and RSP actions.
No credentials. Full histories are not published.
"""
from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

U0 = ["SPY", "QQQ", "IWM", "DIA", "RSP", "MDY"]
U1_ADD = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLC", "XLRE"]
U2_ADD = ["IWF", "IWD", "MTUM", "QUAL", "USMV", "VLUE"]
SYMBOLS = U0 + U1_ADD + U2_ADD
START = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())
END = int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp())
RSP_START = int(datetime(2003, 1, 1, tzinfo=timezone.utc).timestamp())
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"


def fetch_chart(symbol: str, period1: int, period2: int) -> dict:
    params = urllib.parse.urlencode({
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "events": "div,splits,capitalGains",
        "includeAdjustedClose": "true",
        "includePrePost": "false",
    })
    errors = []
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}"
        for attempt in range(5):
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    payload = json.load(r)
                err = payload.get("chart", {}).get("error")
                if err:
                    raise RuntimeError(str(err))
                result = payload.get("chart", {}).get("result")
                if not result:
                    raise RuntimeError("empty chart result")
                return result[0]
            except Exception as exc:
                errors.append(f"{host} attempt={attempt+1}: {type(exc).__name__}: {exc}")
                time.sleep(min(2 ** attempt, 12))
    raise RuntimeError(f"{symbol}: {' | '.join(errors)}")


def rows_from_chart(symbol: str, chart: dict) -> list[dict]:
    ts = chart.get("timestamp") or []
    quote = (chart.get("indicators", {}).get("quote") or [{}])[0]
    adj = (chart.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or [None] * len(ts)
    out = []
    for i, epoch in enumerate(ts):
        def at(name):
            arr = quote.get(name) or []
            return arr[i] if i < len(arr) else None
        out.append({
            "symbol": symbol,
            "trade_date": datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat(),
            "open": at("open"),
            "high": at("high"),
            "low": at("low"),
            "close": at("close"),
            "adj_close": adj[i] if i < len(adj) else None,
            "volume": at("volume"),
            "source": "yahoo_chart_v8",
        })
    return out


def action_rows(symbol: str, chart: dict) -> list[dict]:
    ev = chart.get("events") or {}
    out = []
    for key, typ in (("dividends", "cash_dividend"), ("splits", "split"), ("capitalGains", "capital_gain")):
        for _, item in sorted((ev.get(key) or {}).items(), key=lambda kv: int(kv[0])):
            epoch = item.get("date")
            row = {
                "symbol": symbol,
                "action_type": typ,
                "ex_date": datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat() if epoch else None,
                "cash_amount": item.get("amount"),
                "split_numerator": item.get("numerator"),
                "split_denominator": item.get("denominator"),
                "split_ratio": item.get("splitRatio"),
                "source": "yahoo_chart_v8",
            }
            out.append(row)
    return out


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)


def main() -> None:
    out = Path("tmp/us_etf_r0_output")
    out.mkdir(parents=True, exist_ok=True)
    all_rows, failures = [], {}
    for symbol in SYMBOLS:
        try:
            chart = fetch_chart(symbol, START, END)
            rows = rows_from_chart(symbol, chart)
            if not rows:
                raise RuntimeError("no recent rows")
            all_rows.extend(rows)
            print(symbol, len(rows), rows[0]["trade_date"], rows[-1]["trade_date"], flush=True)
        except Exception as exc:
            failures[symbol] = f"{type(exc).__name__}: {exc}"
    write_csv(out / "u2_recent_yahoo_20260701_20260819.csv", all_rows,
              ["symbol","trade_date","open","high","low","close","adj_close","volume","source"])

    rsp_chart = fetch_chart("RSP", RSP_START, END)
    rsp_actions = action_rows("RSP", rsp_chart)
    write_csv(out / "rsp_actions_yahoo_2003_20260819.csv", rsp_actions,
              ["symbol","action_type","ex_date","cash_amount","split_numerator","split_denominator","split_ratio","source"])

    meta = {
        "symbols": SYMBOLS,
        "recent_start": "2026-07-01",
        "recent_end_inclusive": "2026-08-19",
        "recent_rows": len(all_rows),
        "rsp_actions": len(rsp_actions),
        "failures": failures,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out / "quality.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2), flush=True)
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
