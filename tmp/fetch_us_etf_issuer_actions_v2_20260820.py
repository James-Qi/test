from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import json
import re
import time
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import pandas as pd
import requests

OUT = Path("artifact")
RAW = OUT / "raw"
DUMPS = OUT / "sheet_dumps"
RAW.mkdir(parents=True, exist_ok=True)
DUMPS.mkdir(parents=True, exist_ok=True)
SNAPSHOT_DATE = "2026-08-20"

ISHARES = {
    "IWM": "239710", "IWF": "239706", "IWD": "239708",
    "MTUM": "251614", "QUAL": "256101", "USMV": "239695", "VLUE": "251616",
}
INVESCO = {"RSP": "46137V357", "QQQ": "46090E103"}
SPDR_TARGETS = {
    "SPY", "DIA", "MDY", "XLB", "XLE", "XLF", "XLI", "XLK",
    "XLP", "XLU", "XLV", "XLY", "XLC", "XLRE",
}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
})
fetch_log: list[dict[str, Any]] = []


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, path: Path, *, referer: str | None = None) -> bytes:
    last: Exception | None = None
    for attempt in range(1, 7):
        try:
            headers = {"Referer": referer} if referer else None
            response = session.get(url, headers=headers, timeout=90, allow_redirects=True)
            response.raise_for_status()
            data = response.content
            if not data:
                raise RuntimeError("empty response")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            fetch_log.append({
                "url": url, "path": path.as_posix(), "attempt": attempt, "status": "pass",
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "bytes": len(data), "sha256": sha256_bytes(data), "error": "",
            })
            return data
        except Exception as exc:
            last = exc
            fetch_log.append({
                "url": url, "path": path.as_posix(), "attempt": attempt, "status": "retry",
                "http_status": "", "content_type": "", "bytes": 0, "sha256": "", "error": repr(exc),
            })
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"failed fetch {url}: {last!r}")


def local_name(tag: str) -> str:
    return tag.split("}")[-1].split(":")[-1]


def attr_local(elem: ET.Element, wanted: str) -> str | None:
    for key, value in elem.attrib.items():
        if local_name(key).lower() == wanted.lower():
            return value
    return None


def parse_date(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return pd.Timestamp(value).date().isoformat()
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "-", "--"}:
        return None
    text = text.replace("Sept", "Sep")
    for fmt in (
        "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%m/%d/%Y", "%Y-%m-%d",
        "%m/%d/%y", "%d/%b/%Y", "%d %b %Y", "%d %B %Y",
    ):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return pd.to_datetime(text, errors="raise").date().isoformat()
    except Exception:
        return None


def to_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if not text or text in {"-", "--", "—"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def norm_header(value: Any) -> str:
    text = html.unescape(str(value or "")).lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def pick_col(headers: list[str], predicates: list[tuple[str, ...]]) -> int | None:
    for terms in predicates:
        for index, header in enumerate(headers):
            if all(term in header for term in terms):
                return index
    return None


def worksheet_rows(xml_bytes: bytes) -> dict[str, list[list[str]]]:
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            candidate = xml_bytes.decode(encoding)
            if "<" in candidate:
                text = candidate
                break
        except Exception:
            continue
    if text is None:
        raise RuntimeError("unable to decode SpreadsheetML")
    root = ET.fromstring(text.lstrip("\ufeff"))
    output: dict[str, list[list[str]]] = {}
    for worksheet in root.iter():
        if local_name(worksheet.tag) != "Worksheet":
            continue
        name = attr_local(worksheet, "Name") or f"sheet_{len(output)+1}"
        rows: list[list[str]] = []
        for row in worksheet.iter():
            if local_name(row.tag) != "Row":
                continue
            values: list[str] = []
            cursor = 1
            for cell in list(row):
                if local_name(cell.tag) != "Cell":
                    continue
                explicit_index = attr_local(cell, "Index")
                if explicit_index:
                    target = int(explicit_index)
                    while cursor < target:
                        values.append("")
                        cursor += 1
                cell_value = ""
                for child in cell.iter():
                    if local_name(child.tag) == "Data":
                        cell_value = "" if child.text is None else child.text
                        break
                values.append(cell_value)
                cursor += 1
            rows.append(values)
        output[name] = rows
    return output


def dump_rows(prefix: str, sheets: dict[str, list[list[str]]]) -> None:
    for name, rows in sheets.items():
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "sheet"
        width = max((len(row) for row in rows), default=0)
        with (DUMPS / f"{prefix}__{safe}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for row in rows:
                writer.writerow(row + [""] * (width - len(row)))


def parse_ishares_distributions(ticker: str, data: bytes, source_url: str, source_path: Path) -> list[dict[str, Any]]:
    sheets = worksheet_rows(data)
    dump_rows(f"ishares_{ticker}", sheets)
    candidates = [(name, rows) for name, rows in sheets.items() if "distribution" in name.lower()] or list(sheets.items())
    results: list[dict[str, Any]] = []
    for sheet_name, rows in candidates:
        header_index = None
        for index, row in enumerate(rows):
            headers = [norm_header(value) for value in row]
            has_ex = any("ex_date" in header or ("ex" in header and "date" in header) for header in headers)
            has_distribution = any("distribution" in header for header in headers)
            if has_ex and has_distribution:
                header_index = index
                break
        if header_index is None:
            continue
        headers = [norm_header(value) for value in rows[header_index]]
        i_ex = pick_col(headers, [("ex", "date")])
        i_record = pick_col(headers, [("record", "date")])
        i_pay = pick_col(headers, [("payable", "date"), ("payment", "date"), ("pay", "date")])
        i_total = pick_col(headers, [("total", "distribution"), ("distribution",)])
        i_income = pick_col(headers, [("income",)])
        i_short = pick_col(headers, [("short", "capital"), ("st", "capital")])
        i_long = pick_col(headers, [("long", "capital"), ("lt", "capital")])
        i_roc = pick_col(headers, [("return", "capital")])
        if i_ex is None or i_total is None:
            continue
        for row in rows[header_index + 1:]:
            ex_date = parse_date(row[i_ex]) if i_ex < len(row) else None
            cash_amount = to_float(row[i_total]) if i_total < len(row) else None
            if not ex_date or cash_amount is None:
                continue
            results.append({
                "ticker": ticker, "issuer": "BlackRock/iShares", "ex_date": ex_date,
                "record_date": parse_date(row[i_record]) if i_record is not None and i_record < len(row) else None,
                "pay_date": parse_date(row[i_pay]) if i_pay is not None and i_pay < len(row) else None,
                "cash_amount": cash_amount,
                "ordinary_income": to_float(row[i_income]) if i_income is not None and i_income < len(row) else None,
                "short_term_capital_gain": to_float(row[i_short]) if i_short is not None and i_short < len(row) else None,
                "long_term_capital_gain": to_float(row[i_long]) if i_long is not None and i_long < len(row) else None,
                "return_of_capital": to_float(row[i_roc]) if i_roc is not None and i_roc < len(row) else None,
                "action_type": "cash_dividend",
                "cash_amount_basis": "current_share_basis_issuer_display",
                "source": "ishares_fundDownload_official", "source_url": source_url,
                "source_file": source_path.as_posix(), "source_file_sha256": sha256_bytes(data),
                "source_snapshot_date": SNAPSHOT_DATE, "source_sheet": sheet_name,
            })
        if results:
            break
    return results


def parse_invesco(ticker: str, data: bytes, url: str, path: Path) -> list[dict[str, Any]]:
    payload = json.loads(data)
    items = payload.get("distributions") or []
    output: list[dict[str, Any]] = []
    for item in items:
        ex_date = parse_date(item.get("exDate"))
        cash_amount = to_float(item.get("distributionAmountPerUnit"))
        if not ex_date or cash_amount is None:
            continue
        output.append({
            "ticker": ticker, "issuer": "Invesco", "ex_date": ex_date,
            "record_date": parse_date(item.get("recordDate")), "pay_date": parse_date(item.get("payDate")),
            "cash_amount": cash_amount,
            "ordinary_income": to_float(item.get("ordinaryIncomeDistribution")),
            "short_term_capital_gain": to_float(item.get("shortTermCapitalGainsDistribution")),
            "long_term_capital_gain": to_float(item.get("longTermCapitalGainsDistribution")),
            "return_of_capital": to_float(item.get("returnOfCapitalDistribution")),
            "action_type": "cash_dividend",
            "cash_amount_basis": "issuer_api_as_reported_basis_to_be_audited",
            "source": "invesco_dng_api_official", "source_url": url,
            "source_file": path.as_posix(), "source_file_sha256": sha256_bytes(data),
            "source_snapshot_date": SNAPSHOT_DATE, "source_sheet": "distribution_json",
        })
    return output


def parse_spdr_xlsx(path: Path, data: bytes, source_url: str) -> list[dict[str, Any]]:
    workbook = pd.ExcelFile(path, engine="openpyxl")
    results: list[dict[str, Any]] = []
    for sheet in workbook.sheet_names:
        frame = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object, engine="openpyxl")
        frame.to_csv(DUMPS / f"spdr_historical__{re.sub(r'[^A-Za-z0-9._-]+', '_', sheet)}.csv", index=False, header=False)
        rows = frame.where(pd.notna(frame), "").values.tolist()
        header_index = None
        for index, row in enumerate(rows):
            headers = [norm_header(value) for value in row]
            has_ticker = any("ticker" in header for header in headers)
            has_ex = any("ex_date" in header or ("ex" in header and "date" in header) for header in headers)
            has_distribution = any("distribution" in header for header in headers)
            if has_ticker and has_ex and has_distribution:
                header_index = index
                break
        if header_index is None:
            continue
        headers = [norm_header(value) for value in rows[header_index]]
        i_ticker = pick_col(headers, [("ticker",)])
        i_name = pick_col(headers, [("fund", "name"), ("fund",)])
        i_ex = pick_col(headers, [("ex", "date")])
        i_record = pick_col(headers, [("record", "date")])
        i_pay = pick_col(headers, [("payable", "date"), ("payment", "date"), ("pay", "date")])
        i_total = pick_col(headers, [("total", "distribution"), ("distribution", "amount"), ("distribution",)])
        i_income = pick_col(headers, [("income", "distribution"), ("income",)])
        i_short = pick_col(headers, [("short", "capital"), ("st", "capital")])
        i_long = pick_col(headers, [("long", "capital"), ("lt", "capital")])
        i_roc = pick_col(headers, [("return", "capital")])
        if i_ticker is None or i_ex is None or i_total is None:
            continue
        for row in rows[header_index + 1:]:
            ticker = str(row[i_ticker]).strip().upper() if i_ticker < len(row) else ""
            if ticker not in SPDR_TARGETS:
                continue
            ex_date = parse_date(row[i_ex] if i_ex < len(row) else None)
            cash_amount = to_float(row[i_total] if i_total < len(row) else None)
            if not ex_date or cash_amount is None:
                continue
            results.append({
                "ticker": ticker, "issuer": "State Street/SPDR",
                "fund_name": str(row[i_name]).strip() if i_name is not None and i_name < len(row) else None,
                "ex_date": ex_date,
                "record_date": parse_date(row[i_record]) if i_record is not None and i_record < len(row) else None,
                "pay_date": parse_date(row[i_pay]) if i_pay is not None and i_pay < len(row) else None,
                "cash_amount": cash_amount,
                "ordinary_income": to_float(row[i_income]) if i_income is not None and i_income < len(row) else None,
                "short_term_capital_gain": to_float(row[i_short]) if i_short is not None and i_short < len(row) else None,
                "long_term_capital_gain": to_float(row[i_long]) if i_long is not None and i_long < len(row) else None,
                "return_of_capital": to_float(row[i_roc]) if i_roc is not None and i_roc < len(row) else None,
                "action_type": "cash_dividend",
                "cash_amount_basis": "issuer_workbook_as_reported_basis_to_be_audited",
                "source": "ssga_historical_distributions_official", "source_url": source_url,
                "source_file": path.as_posix(), "source_file_sha256": sha256_bytes(data),
                "source_snapshot_date": SNAPSHOT_DATE, "source_sheet": sheet,
            })
    return results


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def main() -> None:
    all_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for ticker, portfolio_id in ISHARES.items():
        url = (
            "https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
            f"?appSubType=ISHARES&appType=PRODUCT_PAGE&component=fundDownload&locale=en_US&portfolioId={portfolio_id}"
            "&targetSite=us-ishares&userType=individual"
        )
        path = RAW / "ishares" / f"{ticker}_fundDownload.xls"
        try:
            data = fetch(url, path, referer=f"https://www.ishares.com/us/products/{portfolio_id}/")
            rows = parse_ishares_distributions(ticker, data, url, path)
            if not rows:
                raise RuntimeError("no normalized distribution rows")
            all_rows.extend(rows)
        except Exception as exc:
            errors.append({"ticker": ticker, "source": "ishares", "error": repr(exc)})

    for ticker, cusip in INVESCO.items():
        url = (
            f"https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}/distribution"
            "?idType=cusip&productType=ETF&loadType=initial"
        )
        path = RAW / "invesco" / f"{ticker}_distribution.json"
        try:
            data = fetch(url, path, referer="https://www.invesco.com/us/financial-products/etfs")
            rows = parse_invesco(ticker, data, url, path)
            if not rows:
                raise RuntimeError("no normalized distribution rows")
            all_rows.extend(rows)
            nav_url = (
                f"https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}/navs"
                "?idType=cusip&productType=ETF"
            )
            fetch(nav_url, RAW / "invesco" / f"{ticker}_navs.json", referer="https://www.invesco.com/us/financial-products/etfs")
        except Exception as exc:
            errors.append({"ticker": ticker, "source": "invesco", "error": repr(exc)})

    spdr_url = "https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-etf-historical-distributions.xlsx"
    spdr_path = RAW / "ssga" / "spdr-etf-historical-distributions.xlsx"
    try:
        data = fetch(spdr_url, spdr_path, referer="https://www.ssga.com/us/en/individual/resources/documents/etf-dividend-distributions")
        rows = parse_spdr_xlsx(spdr_path, data, spdr_url)
        if not rows:
            raise RuntimeError("no normalized SPDR distribution rows")
        all_rows.extend(rows)
    except Exception as exc:
        errors.append({"ticker": "SPDR_TARGETS", "source": "ssga", "error": repr(exc)})

    extras = {
        "ssga_fundfinder.json": "https://www.ssga.com/bin/v1/ssmp/fund/fundfinder?country=us&language=en&role=intermediary&product=etfs&ui=fund-finder",
        "spdr_product_data.xlsx": "https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-product-data-us-en.xlsx",
        "spdr_distribution_schedule.pdf": "https://www.ssga.com/library-content/products/fund-data/etfs/us/distribution/SPDR_Dividend_Distribution_Schedule.pdf",
    }
    for name, url in extras.items():
        try:
            fetch(url, RAW / "ssga" / name, referer="https://www.ssga.com/us/en/individual/resources/documents/etf-dividend-distributions")
        except Exception as exc:
            errors.append({"ticker": "", "source": name, "error": repr(exc)})

    all_rows.sort(key=lambda row: (row.get("ticker", ""), row.get("ex_date", ""), row.get("cash_amount", 0)))
    write_csv(OUT / "issuer_official_distributions_u2.csv", all_rows)
    write_csv(OUT / "fetch_log.csv", fetch_log)
    write_csv(OUT / "errors.csv", errors)

    summary_by: dict[tuple[str, str], dict[str, Any]] = {}
    for row in all_rows:
        key = (row["ticker"], row["source"])
        item = summary_by.setdefault(key, {
            "ticker": row["ticker"], "source": row["source"], "rows": 0,
            "min_ex_date": row["ex_date"], "max_ex_date": row["ex_date"],
        })
        item["rows"] += 1
        item["min_ex_date"] = min(item["min_ex_date"], row["ex_date"])
        item["max_ex_date"] = max(item["max_ex_date"], row["ex_date"])
    write_csv(OUT / "issuer_official_distribution_summary.csv", sorted(summary_by.values(), key=lambda item: item["ticker"]))

    summary = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "snapshot_date": SNAPSHOT_DATE, "distribution_rows": len(all_rows),
        "tickers": sorted({row["ticker"] for row in all_rows}),
        "ticker_count": len({row["ticker"] for row in all_rows}),
        "errors": errors,
        "fetch_passes": sum(row["status"] == "pass" for row in fetch_log),
        "fetch_retries": sum(row["status"] == "retry" for row in fetch_log),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    hashes = []
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "sha256sums.txt":
            hashes.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(OUT).as_posix()}")
    (OUT / "sha256sums.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if errors:
        print("WARN: source errors are recorded; artifact is still uploaded for audit")


if __name__ == "__main__":
    main()
