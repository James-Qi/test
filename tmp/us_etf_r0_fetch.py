#!/usr/bin/env python3
"""One-shot bounded data fetch for US ETF R0 research."""
from __future__ import annotations
import csv, json, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
U0=["SPY","QQQ","IWM","DIA","RSP","MDY"]
U1=["XLB","XLE","XLF","XLI","XLK","XLP","XLU","XLV","XLY","XLC","XLRE"]
U2=["IWF","IWD","MTUM","QUAL","USMV","VLUE"]
SYMBOLS=U0+U1+U2
START=int(datetime(2026,7,1,tzinfo=timezone.utc).timestamp())
END=int(datetime(2026,8,20,tzinfo=timezone.utc).timestamp())
RSP_START=int(datetime(2003,1,1,tzinfo=timezone.utc).timestamp())
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
def fetch_chart(symbol,p1,p2):
 p=urllib.parse.urlencode({"period1":p1,"period2":p2,"interval":"1d","events":"div,splits,capitalGains","includeAdjustedClose":"true","includePrePost":"false"})
 errors=[]
 for host in ("query2.finance.yahoo.com","query1.finance.yahoo.com"):
  url=f"https://{host}/v8/finance/chart/{urllib.parse.quote(symbol)}?{p}"
  for attempt in range(5):
   try:
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=30) as r: payload=json.load(r)
    result=payload.get("chart",{}).get("result")
    if not result: raise RuntimeError(str(payload.get("chart",{}).get("error") or "empty result"))
    return result[0]
   except Exception as exc:
    errors.append(f"{host} attempt={attempt+1}: {type(exc).__name__}: {exc}"); time.sleep(min(2**attempt,12))
 raise RuntimeError(f"{symbol}: {' | '.join(errors)}")
def bars(symbol,chart):
 ts=chart.get("timestamp") or []; q=(chart.get("indicators",{}).get("quote") or [{}])[0]; a=(chart.get("indicators",{}).get("adjclose") or [{}])[0].get("adjclose") or [None]*len(ts)
 out=[]
 for i,t in enumerate(ts):
  at=lambda n:(q.get(n) or [None]*len(ts))[i]
  out.append({"symbol":symbol,"trade_date":datetime.fromtimestamp(t,tz=timezone.utc).date().isoformat(),"open":at("open"),"high":at("high"),"low":at("low"),"close":at("close"),"adj_close":a[i] if i<len(a) else None,"volume":at("volume"),"source":"yahoo_chart_v8"})
 return out
def actions(symbol,chart):
 out=[]
 for key,typ in (("dividends","cash_dividend"),("splits","split"),("capitalGains","capital_gain")):
  for _,x in sorted(((chart.get("events") or {}).get(key) or {}).items(),key=lambda kv:int(kv[0])):
   t=x.get("date"); out.append({"symbol":symbol,"action_type":typ,"ex_date":datetime.fromtimestamp(t,tz=timezone.utc).date().isoformat() if t else None,"cash_amount":x.get("amount"),"split_numerator":x.get("numerator"),"split_denominator":x.get("denominator"),"split_ratio":x.get("splitRatio"),"source":"yahoo_chart_v8"})
 return out
def write(path,rows,fields):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open("w",newline="",encoding="utf-8") as f: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
def main():
 out=Path("tmp/us_etf_r0_output"); out.mkdir(parents=True,exist_ok=True); all_rows=[]; failures={}
 for s in SYMBOLS:
  try:
   r=bars(s,fetch_chart(s,START,END)); all_rows+=r; print(s,len(r),r[0]["trade_date"],r[-1]["trade_date"],flush=True)
  except Exception as exc: failures[s]=f"{type(exc).__name__}: {exc}"
 write(out/"u2_recent_yahoo_20260701_20260819.csv",all_rows,["symbol","trade_date","open","high","low","close","adj_close","volume","source"])
 ra=actions("RSP",fetch_chart("RSP",RSP_START,END)); write(out/"rsp_actions_yahoo_2003_20260819.csv",ra,["symbol","action_type","ex_date","cash_amount","split_numerator","split_denominator","split_ratio","source"])
 meta={"symbols":SYMBOLS,"recent_start":"2026-07-01","recent_end_inclusive":"2026-08-19","recent_rows":len(all_rows),"rsp_actions":len(ra),"failures":failures,"generated_at_utc":datetime.now(timezone.utc).isoformat()}; (out/"quality.json").write_text(json.dumps(meta,indent=2),encoding="utf-8"); print(json.dumps(meta,indent=2),flush=True)
 if failures: raise SystemExit(2)
if __name__=="__main__": main()
