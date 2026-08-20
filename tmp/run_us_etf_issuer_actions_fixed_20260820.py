#!/usr/bin/env python3
from pathlib import Path

p = Path(__file__).with_name('fetch_us_etf_issuer_actions_20260820.py')
source = p.read_text(encoding='utf-8')
source = source.replace(
    "UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0 Safari/537.36'",
    "UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'",
)
source = source.replace(
    "s.headers.update({'User-Agent':UA,'Accept':'*/*','Accept-Language':'en-US,en;q=0.9'})",
    "s.headers.update({'User-Agent':UA,'Accept':'application/json,*/*','Accept-Language':'en-US,en;q=0.9'})",
)
source = source.replace(
    " text=raw.decode('utf-8-sig',errors='replace')\n root=ET.fromstring(text)",
    " text=raw.decode('utf-8-sig',errors='replace')\n # BlackRock SpreadsheetML contains unescaped ampersands in hyperlink attributes.\n text=re.sub(r'&(?!#\\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]+;)', '&amp;', text)\n root=ET.fromstring(text)",
)
source = source.replace("INVESCO={'RSP':'46137V357','QQQ':'46090E103'}", "INVESCO={}")
source = source.replace("[('navpershare','nav'),('date','asofdate')]", "[('navpershare','nav'),('date','asofdate','asof')]")
exec(compile(source, str(p), 'exec'), {'__name__': '__main__', '__file__': str(p)})
