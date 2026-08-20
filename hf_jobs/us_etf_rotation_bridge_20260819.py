#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "requests>=2.32,<3",
#   "pandas>=2.2,<3",
#   "pyarrow>=20,<26",
#   "huggingface_hub>=0.34,<2",
# ]
# ///
"""One-shot auditable bridge for the frozen US ETF rotation U0/U1/U2 universe.

Outputs:
1) Yahoo chart v8 OHLCV, adjusted close, dividends and splits (raw JSON retained).
2) A filtered 23-symbol extract from HexQuant/Stocks-Daily-Price as an independent
   adjusted-close reference through its source as-of date.
3) QA tables and a SHA256 manifest.

The output repository is intentionally created public only so the research sandbox can
retrieve the one-shot artifact. It must be switched to private immediately afterward.
Yahoo chart v8 is undocumented and is used only as a cached research bridge, not as an
exchange- or issuer-authoritative source.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import random
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds
import requests
from huggingface_hub import HfApi, snapshot_download

SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "RSP", "MDY",
    "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLC", "XLRE",
    "IWF", "IWD", "MTUM", "QUAL", "USMV", "VLUE",
]
UNIVERSES = {
    "U0": ["SPY", "QQQ", "IWM", "DIA", "RSP", "MDY"],
    "U1": [
        "SPY", "QQQ", "IWM", "DIA", "RSP", "MDY",
        "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLC", "XLRE",
    ],
    "U2": SYMBOLS,
}
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OUT_REPO = os.environ.get(
    "OUT_REPO", "jamesqijingsong/us-etf-rotation-r0-bridge-20260819-public"
)
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/tmp/us_etf_rotation_bridge_20260819"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_chart(symbol: str, period2: int, attempts: int = 10) -> dict[str, Any]:
    params = {
        "period1": 0,
        "period2": period2,
        "interval": "1d",
        "events": "capitalGain,div,splits",
        "includeAdjustedClose": "true",
        "includePrePost": "false",
    }
    errors: list[str] = []
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    for attempt in range(attempts):
        host = "query1.finance.yahoo.com" if attempt % 2 == 0 else "query2.finance.yahoo.com"
        url = f"https://{host}/v8/finance/chart/{symbol}"
        try:
            response = session.get(url, params=params, timeout=60)
            if response.status_code == 200:
                payload = response.json()
                chart = payload.get("chart") or {}
                if chart.get("error") is not None:
                    raise RuntimeError(f"Yahoo chart error: {chart['error']}")
                if not chart.get("result"):
                    raise RuntimeError("empty Yahoo chart result")
                return payload
            errors.append(f"{host} attempt={attempt + 1} HTTP {response.status_code}: {response.text[:180]!r}")
        except Exception as exc:
            errors.append(f"attempt={attempt + 1} {type(exc).__name__}: {exc}")
        time.sleep(min(30, 1.7 * 2 ** min(attempt, 4)) + random.random() * 2)
    raise RuntimeError(" | ".join(errors))


def local_date(epoch_s: int, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/New_York")
    return datetime.fromtimestamp(epoch_s, timezone.utc).astimezone(tz).date().isoformat()


def parse_chart(symbol: str, payload: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    result = payload["chart"]["result"][0]
    meta = dict(result.get("meta") or {})
    tz_name = meta.get("exchangeTimezoneName") or "America/New_York"
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adj = (((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or [])

    def values(name: str) -> list[Any]:
        out = list(quote.get(name) or [])
        return out + [None] * max(0, len(timestamps) - len(out))

    opens, highs, lows, closes, volumes = (values(x) for x in ("open", "high", "low", "close", "volume"))
    rows = []
    retrieved = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for i, ts in enumerate(timestamps):
        c = closes[i]
        a = adj[i] if i < len(adj) else None
        rows.append({
            "symbol": symbol,
            "trade_date": local_date(int(ts), tz_name),
            "timestamp_utc": datetime.fromtimestamp(int(ts), timezone.utc).isoformat(),
            "open": opens[i], "high": highs[i], "low": lows[i], "close": c,
            "volume": volumes[i], "adj_close": a,
            "adj_close_over_close": (
                float(a) / float(c)
                if c not in (None, 0) and a is not None and math.isfinite(float(c))
                else None
            ),
            "source": "yahoo_chart_v8",
            "retrieved_at_utc": retrieved,
        })
    bars = pd.DataFrame(rows)

    actions: list[dict[str, Any]] = []
    events = result.get("events") or {}
    for event in (events.get("dividends") or {}).values():
        ts = int(event["date"])
        actions.append({
            "symbol": symbol, "ex_date": local_date(ts, tz_name),
            "record_date": None, "pay_date": None,
            "action_type": "cash_dividend", "cash_amount": event.get("amount"),
            "split_ratio": None, "split_numerator": None, "split_denominator": None,
            "event_timestamp_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "source": "yahoo_chart_v8_events", "quality_flag": "RESEARCH_BRIDGE_UNDOCUMENTED",
        })
    for event in (events.get("capitalGains") or {}).values():
        ts = int(event["date"])
        actions.append({
            "symbol": symbol, "ex_date": local_date(ts, tz_name),
            "record_date": None, "pay_date": None,
            "action_type": "capital_gain_distribution", "cash_amount": event.get("amount"),
            "split_ratio": None, "split_numerator": None, "split_denominator": None,
            "event_timestamp_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "source": "yahoo_chart_v8_events", "quality_flag": "RESEARCH_BRIDGE_UNDOCUMENTED",
        })
    for event in (events.get("splits") or {}).values():
        ts = int(event["date"])
        n, d = event.get("numerator"), event.get("denominator")
        ratio = None
        try:
            ratio = float(n) / float(d) if n is not None and d not in (None, 0) else None
            if ratio is None and event.get("splitRatio") and ":" in str(event["splitRatio"]):
                aa, bb = str(event["splitRatio"]).split(":", 1)
                ratio = float(aa) / float(bb)
        except Exception:
            ratio = None
        actions.append({
            "symbol": symbol, "ex_date": local_date(ts, tz_name),
            "record_date": None, "pay_date": None,
            "action_type": "stock_split", "cash_amount": None,
            "split_ratio": ratio, "split_numerator": n, "split_denominator": d,
            "event_timestamp_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "source": "yahoo_chart_v8_events", "quality_flag": "RESEARCH_BRIDGE_UNDOCUMENTED",
        })
    actions_df = pd.DataFrame(actions)
    meta_row = {
        "symbol": symbol,
        "currency": meta.get("currency"),
        "exchange_name": meta.get("exchangeName"),
        "full_exchange_name": meta.get("fullExchangeName"),
        "instrument_type": meta.get("instrumentType"),
        "first_trade_date_epoch": meta.get("firstTradeDate"),
        "first_trade_date_local": (
            local_date(int(meta["firstTradeDate"]), tz_name)
            if meta.get("firstTradeDate") is not None else None
        ),
        "exchange_timezone": tz_name,
        "data_granularity": meta.get("dataGranularity"),
        "source": "yahoo_chart_v8_meta",
    }
    return bars, actions_df, meta_row


def fetch_yahoo(out: Path) -> dict[str, Any]:
    raw_dir = out / "raw_yahoo_json"
    raw_dir.mkdir(parents=True, exist_ok=True)
    period2 = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
    bars_parts, action_parts, metas, errors = [], [], [], {}
    for i, symbol in enumerate(SYMBOLS, 1):
        print(f"Yahoo [{i:02d}/{len(SYMBOLS)}] {symbol}", flush=True)
        try:
            payload = fetch_chart(symbol, period2)
            with gzip.open(raw_dir / f"{symbol}.json.gz", "wt", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            bars, actions, meta = parse_chart(symbol, payload)
            bars_parts.append(bars)
            if not actions.empty:
                action_parts.append(actions)
            metas.append(meta)
        except Exception as exc:
            errors[symbol] = f"{type(exc).__name__}: {exc}"
            print(errors[symbol], flush=True)
        time.sleep(1.0 + random.random())

    if bars_parts:
        bars = pd.concat(bars_parts, ignore_index=True).sort_values(["symbol", "trade_date"])
    else:
        bars = pd.DataFrame(columns=[
            "symbol", "trade_date", "timestamp_utc", "open", "high", "low", "close",
            "volume", "adj_close", "adj_close_over_close", "source", "retrieved_at_utc",
        ])
    actions = (
        pd.concat(action_parts, ignore_index=True).sort_values(["symbol", "ex_date", "action_type"])
        if action_parts else pd.DataFrame(columns=[
            "symbol", "ex_date", "record_date", "pay_date", "action_type", "cash_amount",
            "split_ratio", "split_numerator", "split_denominator", "event_timestamp_utc",
            "source", "quality_flag",
        ])
    )
    meta = pd.DataFrame(metas).sort_values("symbol") if metas else pd.DataFrame()
    bars.to_parquet(out / "yahoo_us_etf_daily_bars_adjusted_v1.parquet", index=False, compression="zstd")
    bars.to_csv(out / "yahoo_us_etf_daily_bars_adjusted_v1.csv.gz", index=False, compression="gzip")
    actions.to_parquet(out / "yahoo_us_etf_corporate_actions_v1.parquet", index=False, compression="zstd")
    actions.to_csv(out / "yahoo_us_etf_corporate_actions_v1.csv", index=False)
    meta.to_csv(out / "yahoo_us_etf_metadata_v1.csv", index=False)
    qa = []
    for symbol in SYMBOLS:
        g = bars[bars["symbol"] == symbol]
        a = actions[actions["symbol"] == symbol]
        qa.append({
            "symbol": symbol, "rows": len(g),
            "min_date": None if g.empty else g["trade_date"].min(),
            "max_date": None if g.empty else g["trade_date"].max(),
            "duplicate_dates": int(g.duplicated(["symbol", "trade_date"]).sum()),
            "null_close": int(g["close"].isna().sum()),
            "null_adj_close": int(g["adj_close"].isna().sum()),
            "cash_distributions": int(a["action_type"].isin(["cash_dividend", "capital_gain_distribution"]).sum()),
            "split_events": int((a["action_type"] == "stock_split").sum()),
            "fetch_error": errors.get(symbol),
        })
    pd.DataFrame(qa).to_csv(out / "yahoo_fetch_qa_v1.csv", index=False)
    (out / "yahoo_fetch_errors_v1.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"bars_rows": len(bars), "actions_rows": len(actions), "errors": errors, "period2": period2}


def fetch_hexquant(out: Path) -> dict[str, Any]:
    local = out / "_hexquant_snapshot"
    print("Downloading HexQuant/Stocks-Daily-Price source shards...", flush=True)
    snapshot_download(
        repo_id="HexQuant/Stocks-Daily-Price",
        repo_type="dataset",
        allow_patterns=["data/*.parquet", "README.md"],
        local_dir=local,
    )
    files = sorted((local / "data").glob("*.parquet"))
    if not files:
        raise RuntimeError("HexQuant parquet shards not found")
    dataset = ds.dataset([str(p) for p in files], format="parquet")
    table = dataset.to_table(
        columns=["symbol", "date", "open", "high", "low", "close", "volume", "adj_close"],
        filter=pc.field("symbol").isin(SYMBOLS),
    )
    df = table.to_pandas().sort_values(["symbol", "date"])
    df["source"] = "HexQuant/Stocks-Daily-Price"
    df["source_revision_note"] = "one-commit duplicate of paperswithbacktest; license=other"
    df.to_parquet(out / "hexquant_us_etf_adjusted_reference_v1.parquet", index=False, compression="zstd")
    df.to_csv(out / "hexquant_us_etf_adjusted_reference_v1.csv.gz", index=False, compression="gzip")
    qa = df.groupby("symbol", as_index=False).agg(rows=("date", "size"), min_date=("date", "min"), max_date=("date", "max"))
    qa.to_csv(out / "hexquant_extract_qa_v1.csv", index=False)
    shutil.rmtree(local, ignore_errors=True)
    return {"rows": len(df), "symbols": sorted(df["symbol"].unique().tolist())}


def write_manifest(out: Path, yahoo: dict[str, Any], hexquant: dict[str, Any] | None, errors: dict[str, str]) -> None:
    files = {}
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "manifest_bridge_v1.json":
            files[str(path.relative_to(out))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
    manifest = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "requested_symbols": SYMBOLS,
        "universes": UNIVERSES,
        "yahoo": yahoo,
        "hexquant": hexquant,
        "secondary_source_errors": errors,
        "temporary_public_repo": OUT_REPO,
        "governance_note": "Switch repository to private immediately after sandbox retrieval.",
        "files": files,
    }
    (out / "manifest_bridge_v1.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def upload(out: Path) -> None:
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN was not passed to the Job")
    api = HfApi(token=token)
    api.create_repo(repo_id=OUT_REPO, repo_type="dataset", private=False, exist_ok=True)
    readme = f"""---
license: other
---
# Temporary US ETF rotation R0 bridge

One-shot auditable research bridge generated at {datetime.now(timezone.utc).isoformat()} for the frozen 23-symbol U0/U1/U2 universe.

**Governance:** temporary public transport repository; switch to private immediately after sandbox retrieval. Yahoo chart v8 is undocumented. HexQuant source license is `other`. Do not treat either as an exchange/issuer-authoritative production feed.
"""
    (out / "README.md").write_text(readme, encoding="utf-8")
    api.upload_folder(
        folder_path=str(out),
        repo_id=OUT_REPO,
        repo_type="dataset",
        path_in_repo="bridge_v1",
        commit_message="data: publish one-shot US ETF rotation R0 bridge",
    )


def main() -> None:
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    yahoo = fetch_yahoo(OUTPUT_DIR)
    secondary_errors: dict[str, str] = {}
    hexquant: dict[str, Any] | None = None
    try:
        hexquant = fetch_hexquant(OUTPUT_DIR)
    except Exception as exc:
        secondary_errors["HexQuant/Stocks-Daily-Price"] = f"{type(exc).__name__}: {exc}"
        print(secondary_errors["HexQuant/Stocks-Daily-Price"], flush=True)
    write_manifest(OUTPUT_DIR, yahoo, hexquant, secondary_errors)
    upload(OUTPUT_DIR)
    print(json.dumps({"repo": OUT_REPO, "yahoo": yahoo, "hexquant": hexquant, "errors": secondary_errors}, indent=2))
    if len(yahoo["errors"]) == len(SYMBOLS):
        raise SystemExit("all Yahoo symbols failed")


if __name__ == "__main__":
    main()
