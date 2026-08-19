#!/usr/bin/env python3
"""Fetch and freeze Yahoo daily history/actions for the R0 US ETF universe.

This is a source-completion bridge, not the research engine. Yahoo's vendor
Close is retained for independent validation; it is not relabeled as nominal
raw execution price. The primary raw OHLC base remains the TDX package.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yfinance as yf

HISTORY_COLUMNS = [
    "symbol", "trade_date", "vendor_open", "vendor_high", "vendor_low",
    "vendor_close", "vendor_adj_close", "volume", "cash_dividend",
    "stock_split_ratio", "capital_gain", "vendor_adj_factor",
    "source", "retrieved_at_utc",
]
ACTION_COLUMNS = [
    "symbol", "ex_date", "action_type", "cash_amount", "split_ratio",
    "capital_gain_amount", "source", "retrieved_at_utc", "quality_flag",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def fetch_one(symbol: str, start: str, end: str, retries: int = 4) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            frame = yf.Ticker(symbol).history(
                start=start,
                end=end,
                interval="1d",
                auto_adjust=False,
                back_adjust=False,
                actions=True,
                repair=False,
                keepna=False,
                timeout=30,
            )
            if frame is None or frame.empty:
                raise RuntimeError(f"empty history for {symbol}")
            return frame.copy()
        except Exception as exc:  # provider/network retries are expected occasionally
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"failed to fetch {symbol}: {last_error}")


def normalize_history(symbol: str, frame: pd.DataFrame, retrieved_at: str) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"{symbol}: missing columns {missing}")

    dates = pd.to_datetime(frame.index)
    if getattr(dates, "tz", None) is not None:
        dates = dates.tz_convert("America/New_York").tz_localize(None)
    trade_date = dates.strftime("%Y-%m-%d")

    def col(name: str, default: float = 0.0) -> pd.Series:
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce")
        return pd.Series(default, index=frame.index, dtype="float64")

    close = col("Close")
    adj_close = col("Adj Close", float("nan"))
    adj_factor = adj_close / close.where(close != 0)

    hist = pd.DataFrame(
        {
            "symbol": symbol,
            "trade_date": trade_date,
            "vendor_open": col("Open").to_numpy(),
            "vendor_high": col("High").to_numpy(),
            "vendor_low": col("Low").to_numpy(),
            "vendor_close": close.to_numpy(),
            "vendor_adj_close": adj_close.to_numpy(),
            "volume": col("Volume").fillna(0).round().astype("int64").to_numpy(),
            "cash_dividend": col("Dividends").fillna(0.0).to_numpy(),
            "stock_split_ratio": col("Stock Splits").fillna(0.0).to_numpy(),
            "capital_gain": col("Capital Gains").fillna(0.0).to_numpy(),
            "vendor_adj_factor": adj_factor.to_numpy(),
            "source": "yahoo_chart_via_yfinance",
            "retrieved_at_utc": retrieved_at,
        }
    )[HISTORY_COLUMNS]

    numeric_price = ["vendor_open", "vendor_high", "vendor_low", "vendor_close", "vendor_adj_close"]
    for c in numeric_price:
        hist[c] = pd.to_numeric(hist[c], errors="coerce")
    hist = hist.dropna(subset=["trade_date", "vendor_close"]).drop_duplicates(["symbol", "trade_date"], keep="last")

    action_rows: list[dict[str, Any]] = []
    for row in hist.itertuples(index=False):
        if row.cash_dividend and abs(float(row.cash_dividend)) > 0:
            action_rows.append(
                {
                    "symbol": symbol,
                    "ex_date": row.trade_date,
                    "action_type": "cash_dividend",
                    "cash_amount": float(row.cash_dividend),
                    "split_ratio": None,
                    "capital_gain_amount": None,
                    "source": "yahoo_chart_via_yfinance",
                    "retrieved_at_utc": retrieved_at,
                    "quality_flag": "vendor_event_unverified",
                }
            )
        if row.stock_split_ratio and abs(float(row.stock_split_ratio)) > 0:
            action_rows.append(
                {
                    "symbol": symbol,
                    "ex_date": row.trade_date,
                    "action_type": "stock_split",
                    "cash_amount": None,
                    "split_ratio": float(row.stock_split_ratio),
                    "capital_gain_amount": None,
                    "source": "yahoo_chart_via_yfinance",
                    "retrieved_at_utc": retrieved_at,
                    "quality_flag": "vendor_event_unverified",
                }
            )
        if row.capital_gain and abs(float(row.capital_gain)) > 0:
            action_rows.append(
                {
                    "symbol": symbol,
                    "ex_date": row.trade_date,
                    "action_type": "capital_gain",
                    "cash_amount": None,
                    "split_ratio": None,
                    "capital_gain_amount": float(row.capital_gain),
                    "source": "yahoo_chart_via_yfinance",
                    "retrieved_at_utc": retrieved_at,
                    "quality_flag": "vendor_event_unverified",
                }
            )
    actions = pd.DataFrame(action_rows, columns=ACTION_COLUMNS)

    stats = {
        "symbol": symbol,
        "rows": int(len(hist)),
        "min_date": str(hist["trade_date"].min()),
        "max_date": str(hist["trade_date"].max()),
        "cash_dividend_events": int((hist["cash_dividend"].fillna(0) != 0).sum()),
        "stock_split_events": int((hist["stock_split_ratio"].fillna(0) != 0).sum()),
        "capital_gain_events": int((hist["capital_gain"].fillna(0) != 0).sum()),
        "null_adj_close": int(hist["vendor_adj_close"].isna().sum()),
        "duplicate_keys": int(hist.duplicated(["symbol", "trade_date"]).sum()),
    }
    return hist, actions, stats


def write_csv_gz_deterministic(frame: pd.DataFrame, path: Path) -> None:
    raw = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    with path.open("wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz:
            gz.write(raw)


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_table(table, path, compression="zstd", compression_level=9, use_dictionary=True, write_statistics=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True, type=Path)
    ap.add_argument("--output-dir", required=True, type=Path)
    args = ap.parse_args()

    request = json.loads(args.request.read_text(encoding="utf-8"))
    symbols = list(dict.fromkeys(str(s).upper() for s in request["symbols"]))
    start = request["start"]
    end = request["end_exclusive"]
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    histories: list[pd.DataFrame] = []
    actions: list[pd.DataFrame] = []
    stats: list[dict[str, Any]] = []
    for symbol in symbols:
        print(f"Fetching {symbol} ...", flush=True)
        raw = fetch_one(symbol, start, end)
        hist, acts, stat = normalize_history(symbol, raw, retrieved_at)
        histories.append(hist)
        actions.append(acts)
        stats.append(stat)

    history = pd.concat(histories, ignore_index=True).sort_values(["symbol", "trade_date"], kind="stable")
    corporate_actions = pd.concat(actions, ignore_index=True) if actions else pd.DataFrame(columns=ACTION_COLUMNS)
    corporate_actions = corporate_actions.sort_values(["symbol", "ex_date", "action_type"], kind="stable")

    expected = set(symbols)
    observed = set(history["symbol"].unique())
    if observed != expected:
        raise RuntimeError(f"symbol coverage mismatch: missing={sorted(expected-observed)}, extra={sorted(observed-expected)}")
    if history.duplicated(["symbol", "trade_date"]).any():
        raise RuntimeError("duplicate history keys after normalization")

    stem = request["dataset_id"]
    outputs = {
        "history_csv_gz": args.output_dir / f"{stem}_history.csv.gz",
        "history_parquet": args.output_dir / f"{stem}_history.parquet",
        "actions_csv_gz": args.output_dir / f"{stem}_corporate_actions.csv.gz",
        "actions_parquet": args.output_dir / f"{stem}_corporate_actions.parquet",
        "metadata": args.output_dir / f"{stem}_metadata.json",
        "sha256sums": args.output_dir / "SHA256SUMS.txt",
    }
    write_csv_gz_deterministic(history, outputs["history_csv_gz"])
    write_parquet(history, outputs["history_parquet"])
    write_csv_gz_deterministic(corporate_actions, outputs["actions_csv_gz"])
    write_parquet(corporate_actions, outputs["actions_parquet"])

    metadata = {
        "dataset_id": stem,
        "request": request,
        "retrieved_at_utc": retrieved_at,
        "source_semantics": {
            "vendor_ohlc": "Yahoo chart history with auto_adjust=False; retained only as a secondary vendor series.",
            "primary_execution_price": "TDX raw/unadjusted OHLC, outside this bridge.",
            "vendor_adj_close": "Yahoo adjusted close; used for independent total-return validation, not silently substituted for TDX raw OHLC.",
            "corporate_actions": "Yahoo chart events. Every event remains quality_flag=vendor_event_unverified until cross-source checks pass.",
        },
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "pandas": pd.__version__,
            "pyarrow": pa.__version__,
            "yfinance": yf.__version__,
        },
        "history_rows": int(len(history)),
        "action_rows": int(len(corporate_actions)),
        "symbols": symbols,
        "symbol_stats": stats,
    }
    outputs["metadata"].write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    checksums = []
    for key, path in outputs.items():
        if key == "sha256sums":
            continue
        checksums.append(f"{sha256_file(path)}  {path.name}")
    outputs["sha256sums"].write_text("\n".join(checksums) + "\n", encoding="utf-8")

    print(json.dumps({"history_rows": len(history), "action_rows": len(corporate_actions), "files": [p.name for p in outputs.values()]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
