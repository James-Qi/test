from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import json
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "RSP", "MDY",
    "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLC", "XLRE",
    "IWF", "IWD", "MTUM", "QUAL", "USMV", "VLUE",
]
CUTOFF = dt.date(2026, 8, 19)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0 Safari/537.36"
OUT = Path("artifact")
RAW = OUT / "raw_json"
RAW.mkdir(parents=True, exist_ok=True)
NY = ZoneInfo("America/New_York")


def fetch_symbol(symbol: str, fetch_log: list[dict]) -> bytes:
    endpoints = ["query2.finance.yahoo.com", "query1.finance.yahoo.com"]
    last_error = None
    for attempt in range(1, 7):
        host = endpoints[(attempt - 1) % len(endpoints)]
        url = (
            f"https://{host}/v8/finance/chart/{symbol}"
            "?range=max&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
            obj = json.loads(payload)
            err = obj.get("chart", {}).get("error")
            if err:
                raise RuntimeError(f"Yahoo chart error: {err}")
            if not obj.get("chart", {}).get("result"):
                raise RuntimeError("Yahoo chart result is empty")
            fetch_log.append(
                {"symbol": symbol, "attempt": attempt, "host": host, "status": "pass", "bytes": len(payload), "error": ""}
            )
            return payload
        except Exception as exc:  # noqa: BLE001 - audit log preserves exact vendor failure
            last_error = repr(exc)
            fetch_log.append(
                {"symbol": symbol, "attempt": attempt, "host": host, "status": "retry", "bytes": 0, "error": last_error}
            )
            time.sleep(min(2**attempt, 20))
    raise RuntimeError(f"failed {symbol}: {last_error}")


def main() -> None:
    bars: list[dict] = []
    actions: list[dict] = []
    symbol_summary: list[dict] = []
    fetch_log: list[dict] = []

    for idx, symbol in enumerate(SYMBOLS, 1):
        payload = fetch_symbol(symbol, fetch_log)
        (RAW / f"{symbol}.json").write_bytes(payload)
        root = json.loads(payload)["chart"]["result"][0]
        meta = root.get("meta", {})
        timestamps = root.get("timestamp") or []
        quote = (root.get("indicators", {}).get("quote") or [{}])[0]
        adjclose = (root.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
        n = len(timestamps)
        if not all(len(quote.get(k) or []) == n for k in ["open", "high", "low", "close", "volume"]):
            raise RuntimeError(f"{symbol}: OHLCV array length mismatch")
        if len(adjclose) != n:
            raise RuntimeError(f"{symbol}: adjclose length mismatch")

        symbol_rows: list[dict] = []
        for i, ts in enumerate(timestamps):
            trade_date = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(NY).date()
            if trade_date > CUTOFF:
                continue
            row = {
                "ticker": symbol,
                "trade_date": trade_date.isoformat(),
                "timestamp_utc": int(ts),
                "open": quote["open"][i],
                "high": quote["high"][i],
                "low": quote["low"][i],
                "close": quote["close"][i],
                "adj_close": adjclose[i],
                "volume": quote["volume"][i],
                "currency": meta.get("currency"),
                "exchange_name": meta.get("exchangeName"),
                "instrument_type": meta.get("instrumentType"),
                "source": "yahoo_chart_v8",
                "source_snapshot_date": CUTOFF.isoformat(),
            }
            bars.append(row)
            symbol_rows.append(row)

        symbol_actions: list[dict] = []
        events = root.get("events") or {}
        for event_type, key in [("cash_dividend", "dividends"), ("split", "splits")]:
            for ev in (events.get(key) or {}).values():
                ts = int(ev.get("date"))
                event_date = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(NY).date()
                if event_date > CUTOFF:
                    continue
                action = {
                    "ticker": symbol,
                    "event_date": event_date.isoformat(),
                    "timestamp_utc": ts,
                    "action_type": event_type,
                    "cash_amount": ev.get("amount") if event_type == "cash_dividend" else None,
                    "split_numerator": ev.get("numerator") if event_type == "split" else None,
                    "split_denominator": ev.get("denominator") if event_type == "split" else None,
                    "split_ratio": ev.get("splitRatio") if event_type == "split" else None,
                    "source": "yahoo_chart_v8",
                    "source_snapshot_date": CUTOFF.isoformat(),
                }
                actions.append(action)
                symbol_actions.append(action)

        symbol_summary.append(
            {
                "ticker": symbol,
                "rows": len(symbol_rows),
                "min_date": symbol_rows[0]["trade_date"] if symbol_rows else None,
                "max_date": symbol_rows[-1]["trade_date"] if symbol_rows else None,
                "dividend_events": sum(a["action_type"] == "cash_dividend" for a in symbol_actions),
                "split_events": sum(a["action_type"] == "split" for a in symbol_actions),
                "raw_json_sha256": hashlib.sha256(payload).hexdigest(),
                "raw_json_bytes": len(payload),
            }
        )
        print(f"[{idx:02d}/{len(SYMBOLS)}] {symbol}: {len(symbol_rows)} rows, {len(symbol_actions)} actions", flush=True)
        time.sleep(1.0)

    bars.sort(key=lambda r: (r["ticker"], r["trade_date"]))
    actions.sort(key=lambda r: (r["ticker"], r["event_date"], r["action_type"]))

    bar_fields = [
        "ticker", "trade_date", "timestamp_utc", "open", "high", "low", "close", "adj_close", "volume",
        "currency", "exchange_name", "instrument_type", "source", "source_snapshot_date",
    ]
    with gzip.open(OUT / "yahoo_u2_daily_1990_20260819.csv.gz", "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=bar_fields)
        writer.writeheader()
        writer.writerows(bars)

    action_fields = [
        "ticker", "event_date", "timestamp_utc", "action_type", "cash_amount",
        "split_numerator", "split_denominator", "split_ratio", "source", "source_snapshot_date",
    ]
    with open(OUT / "yahoo_u2_corporate_actions_1990_20260819.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=action_fields)
        writer.writeheader()
        writer.writerows(actions)

    with open(OUT / "yahoo_u2_symbol_summary.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(symbol_summary[0]))
        writer.writeheader()
        writer.writerows(symbol_summary)

    with open(OUT / "fetch_log.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "attempt", "host", "status", "bytes", "error"])
        writer.writeheader()
        writer.writerows(fetch_log)

    summary = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cutoff_date": CUTOFF.isoformat(),
        "symbols": SYMBOLS,
        "symbol_count": len(SYMBOLS),
        "bar_rows": len(bars),
        "corporate_action_rows": len(actions),
        "cash_dividend_events": sum(a["action_type"] == "cash_dividend" for a in actions),
        "split_events": sum(a["action_type"] == "split" for a in actions),
        "source": "Yahoo chart v8 endpoint; cached once for research audit",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    hashes: list[str] = []
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "sha256sums.txt":
            hashes.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(OUT).as_posix()}")
    (OUT / "sha256sums.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
