#!/usr/bin/env python3
from __future__ import annotations
import csv, hashlib, json, re, sys, time
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

OUT=Path('artifact')
RAW=OUT/'raw'
RAW.mkdir(parents=True, exist_ok=True)
ASOF='2026-08-19'
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0 Safari/537.36'
ISHARES={
 'IWF':'239706','IWD':'239708','IWM':'239710','MTUM':'251614',
 'QUAL':'256101','USMV':'239695','VLUE':'251616',
}
INVESCO={'RSP':'46137V357','QQQ':'46090E103'}

s=requests.Session()
retry=Retry(total=5,connect=5,read=5,status=5,backoff_factor=1.2,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset(['GET']))
s.mount('https://',HTTPAdapter(max_retries=retry))
s.headers.update({'User-Agent':UA,'Accept':'*/*','Accept-Language':'en-US,en;q=0.9'})

def sha(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''): h.update(b)
 return h.hexdigest()

def fetch(url:str, *, referer:str|None=None)->bytes:
 hdr={}
 if referer: hdr['Referer']=referer
 r=s.get(url,headers=hdr,timeout=90)
 r.raise_for_status()
 if not r.content: raise RuntimeError(f'empty response {url}')
 return r.content

def lname(tag:str)->str: return tag.rsplit('}',1)[-1]
def attr_local(el:ET.Element,name:str)->str|None:
 for k,v in el.attrib.items():
  if k.rsplit('}',1)[-1]==name: return v
 return None

def parse_rows(raw:bytes)->dict[str,list[list[str]]]:
 text=raw.decode('utf-8-sig',errors='replace')
 root=ET.fromstring(text)
 sheets={}
 for ws in root.iter():
  if lname(ws.tag)!='Worksheet': continue
  nm=attr_local(ws,'Name') or ''
  rows=[]
  for row in ws.iter():
   if lname(row.tag)!='Row': continue
   vals=[]; col=1
   for cell in list(row):
    if lname(cell.tag)!='Cell': continue
    idx=attr_local(cell,'Index')
    if idx:
     tgt=int(idx)
     while col<tgt: vals.append(''); col+=1
    data=''
    for ch in cell.iter():
     if lname(ch.tag)=='Data': data=''.join(ch.itertext()).strip(); break
    vals.append(data); col+=1
   if vals: rows.append(vals)
  sheets[nm]=rows
 return sheets

def norm(sv:str)->str: return re.sub(r'[^a-z0-9]+','',sv.lower())
def to_float(x:str)->float|None:
 x=(x or '').strip().replace(',','').replace('$','')
 if not x or x in {'-','--','—','N/A','NA'}: return None
 try:return float(x)
 except:return None

def parse_date(x:str)->str|None:
 from datetime import datetime
 x=(x or '').strip()
 fmts=['%b %d, %Y','%d-%b-%Y','%d/%b/%Y','%Y-%m-%d','%m/%d/%Y','%m/%d/%y']
 for fmt in fmts:
  try:return datetime.strptime(x,fmt).date().isoformat()
  except:pass
 try:
  v=float(x)
  if 20000<v<80000:
   from datetime import date,timedelta
   return (date(1899,12,30)+timedelta(days=v)).isoformat()
 except:pass
 return None

def find_header(rows, required_groups):
 for i,row in enumerate(rows):
  ns=[norm(x) for x in row]
  ok=True
  for group in required_groups:
   if not any(any(token in c for token in group) for c in ns): ok=False; break
  if ok:return i,row
 return None,None

def col_index(header, candidates):
 ns=[norm(x) for x in header]
 for j,c in enumerate(ns):
  if any(q in c for q in candidates):return j
 return None

def get(row,j): return row[j] if j is not None and j<len(row) else ''

all_dist=[]; all_nav=[]; files=[]; errors=[]; summaries=[]
for ticker,pid in ISHARES.items():
 url=('https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document'
      f'?appSubType=ISHARES&appType=PRODUCT_PAGE&component=fundDownload&locale=en_US&portfolioId={pid}'
      '&targetSite=us-ishares&userType=individual')
 try:
  raw=fetch(url,referer=f'https://www.ishares.com/us/products/{pid}/')
  p=RAW/f'ishares_{ticker}_{pid}_fundDownload.xls'; p.write_bytes(raw)
  files.append({'ticker':ticker,'issuer':'BlackRock/iShares','raw_file':str(p.relative_to(OUT)),'bytes':p.stat().st_size,'sha256':sha(p),'url':url})
  sheets=parse_rows(raw)
  dname=next((n for n in sheets if norm(n)=='distributions'),None)
  hname=next((n for n in sheets if norm(n)=='historical'),None)
  if not dname: raise RuntimeError(f'{ticker}: Distributions sheet absent; sheets={list(sheets)}')
  rows=sheets[dname]
  hi,header=find_header(rows,[('recorddate','record'),('exdate','ex'),('totaldistribution','distribution')])
  if hi is None: raise RuntimeError(f'{ticker}: distribution header absent')
  jrec=col_index(header,('recorddate',)); jex=col_index(header,('exdate',)); jpay=col_index(header,('payabledate','paydate'))
  jtot=col_index(header,('totaldistribution','distributionamount')); jinc=col_index(header,('income',)); jst=col_index(header,('stcapgains','shortterm'))
  jlt=col_index(header,('ltcapgains','longterm')); jroc=col_index(header,('returnofcapital',))
  n=0
  for row in rows[hi+1:]:
   ex=parse_date(get(row,jex)); amt=to_float(get(row,jtot))
   if not ex or amt is None: continue
   all_dist.append({'ticker':ticker,'ex_date':ex,'record_date':parse_date(get(row,jrec)),'pay_date':parse_date(get(row,jpay)),
      'action_type':'cash_dividend','cash_amount_current_share_basis':amt,'ordinary_income':to_float(get(row,jinc)),
      'short_term_cap_gain':to_float(get(row,jst)),'long_term_cap_gain':to_float(get(row,jlt)),'return_of_capital':to_float(get(row,jroc)),
      'issuer':'BlackRock/iShares','issuer_product_id':pid,'issuer_cusip':'','source':'issuer_official_ishares_fundDownload',
      'source_snapshot_date':ASOF,'amount_basis':'current_share_split_adjusted_history'})
   n+=1
  navn=0
  if hname:
   rows=sheets[hname]
   hi,header=find_header(rows,[('navpershare','nav'),('date','asofdate')])
   if hi is not None:
    jdate=col_index(header,('asofdate','date')); jnav=col_index(header,('navpershare','nav'))
    jdiv=col_index(header,('exdividends','exdividend')); jshares=col_index(header,('sharesoutstanding','shares'))
    for row in rows[hi+1:]:
     d=parse_date(get(row,jdate)); nv=to_float(get(row,jnav))
     if not d or nv is None:continue
     all_nav.append({'ticker':ticker,'trade_date':d,'nav_per_share_current_basis':nv,'ex_dividends_current_basis':to_float(get(row,jdiv)),
       'shares_outstanding':to_float(get(row,jshares)),'issuer_product_id':pid,'source':'issuer_official_ishares_fundDownload','source_snapshot_date':ASOF})
     navn+=1
  summaries.append({'ticker':ticker,'issuer':'BlackRock/iShares','distribution_rows':n,'nav_rows':navn,'status':'pass'})
 except Exception as e:
  errors.append({'ticker':ticker,'issuer':'BlackRock/iShares','error':repr(e)})
  summaries.append({'ticker':ticker,'issuer':'BlackRock/iShares','distribution_rows':0,'nav_rows':0,'status':'error'})
 time.sleep(.5)

def find_distributions(obj:Any):
 if isinstance(obj,dict):
  for k,v in obj.items():
   if k.lower()=='distributions' and isinstance(v,list): return v
  for v in obj.values():
   hit=find_distributions(v)
   if hit is not None:return hit
 elif isinstance(obj,list):
  for v in obj:
   hit=find_distributions(v)
   if hit is not None:return hit
 return None

def pick(d,*names):
 low={k.lower():v for k,v in d.items()}
 for n in names:
  if n.lower() in low:return low[n.lower()]
 return None

for ticker,cusip in INVESCO.items():
 url=f'https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}/distribution?idType=cusip&productType=ETF&loadType=initial'
 try:
  raw=fetch(url,referer='https://www.invesco.com/')
  p=RAW/f'invesco_{ticker}_{cusip}_distribution.json'; p.write_bytes(raw)
  files.append({'ticker':ticker,'issuer':'Invesco','raw_file':str(p.relative_to(OUT)),'bytes':p.stat().st_size,'sha256':sha(p),'url':url})
  obj=json.loads(raw)
  ds=find_distributions(obj)
  if not ds: raise RuntimeError(f'{ticker}: no distributions list; top={list(obj) if isinstance(obj,dict) else type(obj)}')
  n=0
  for d in ds:
   if not isinstance(d,dict):continue
   ex=parse_date(str(pick(d,'exDate','ex_date') or ''))
   amt=to_float(str(pick(d,'distributionAmountPerUnit','totalDistribution','distributionAmount','rate') or ''))
   if not ex or amt is None:continue
   all_dist.append({'ticker':ticker,'ex_date':ex,'record_date':parse_date(str(pick(d,'recordDate') or '')),
    'pay_date':parse_date(str(pick(d,'payDate','payableDate') or '')),'action_type':'cash_dividend',
    'cash_amount_current_share_basis':amt,'ordinary_income':to_float(str(pick(d,'ordinaryIncomeDistribution') or '')),
    'short_term_cap_gain':to_float(str(pick(d,'shortTermCapitalGainsDistribution') or '')),
    'long_term_cap_gain':to_float(str(pick(d,'longTermCapitalGainsDistribution') or '')),
    'return_of_capital':to_float(str(pick(d,'returnOfCapitalDistribution') or '')),
    'issuer':'Invesco','issuer_product_id':'','issuer_cusip':cusip,'source':'issuer_official_invesco_dng_api',
    'source_snapshot_date':ASOF,'amount_basis':'issuer_reported_to_be_determined_by_split_crosscheck'})
   n+=1
  summaries.append({'ticker':ticker,'issuer':'Invesco','distribution_rows':n,'nav_rows':0,'status':'pass'})
 except Exception as e:
  errors.append({'ticker':ticker,'issuer':'Invesco','error':repr(e)})
  summaries.append({'ticker':ticker,'issuer':'Invesco','distribution_rows':0,'nav_rows':0,'status':'error'})
 time.sleep(.5)

def write_csv(path,rows,fields):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)

all_dist=sorted(all_dist,key=lambda r:(r['ticker'],r['ex_date']))
all_nav=sorted(all_nav,key=lambda r:(r['ticker'],r['trade_date']))
write_csv(OUT/'issuer_cash_distributions_current_share_basis.csv',all_dist,list(all_dist[0]) if all_dist else ['ticker'])
write_csv(OUT/'ishares_nav_history_current_share_basis.csv',all_nav,list(all_nav[0]) if all_nav else ['ticker'])
write_csv(OUT/'fetch_inventory.csv',files,list(files[0]) if files else ['ticker'])
write_csv(OUT/'symbol_summary.csv',summaries,['ticker','issuer','distribution_rows','nav_rows','status'])
write_csv(OUT/'errors.csv',errors,['ticker','issuer','error'])
summary={'as_of_date':ASOF,'symbols':sorted(set(ISHARES)|set(INVESCO)),'distribution_rows':len(all_dist),'nav_rows':len(all_nav),
         'raw_files':len(files),'errors':errors,'symbol_summary':summaries}
(OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
counts={t:0 for t in set(ISHARES)|set(INVESCO)}
for r in all_dist: counts[r['ticker']]+=1
bad={t:n for t,n in counts.items() if n<20}
if errors or bad:
 print(json.dumps(summary,indent=2)); raise SystemExit(f'issuer bridge failed errors={errors} insufficient={bad}')
print(json.dumps(summary,indent=2))
