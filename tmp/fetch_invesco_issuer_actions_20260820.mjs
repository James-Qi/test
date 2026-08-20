import fs from 'node:fs';
import crypto from 'node:crypto';

const OUT = 'artifact';
const RAW = `${OUT}/raw`;
fs.mkdirSync(RAW, { recursive: true });
const funds = { RSP: '46137V357', QQQ: '46090E103' };
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getJson(url) {
  let body = '';
  for (let attempt = 0; attempt <= 4; attempt++) {
    const res = await fetch(url, {
      headers: {
        'User-Agent': UA,
        'Accept': 'application/json,*/*',
        'Referer': 'https://www.invesco.com/',
      },
    });
    body = await res.text();
    if (res.ok) return { json: JSON.parse(body), body };
    if (res.status >= 500 && attempt < 4) {
      await sleep(250 * (attempt + 1));
      continue;
    }
    throw new Error(`HTTP ${res.status} ${url}: ${body.slice(0, 300)}`);
  }
  throw new Error(`exhausted retries ${url}: ${body.slice(0, 300)}`);
}

const rows = [];
const inventory = [];
for (const [ticker, cusip] of Object.entries(funds)) {
  const url = `https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/${cusip}/distribution?idType=cusip&productType=ETF&loadType=initial`;
  const { json, body } = await getJson(url);
  const rawPath = `${RAW}/invesco_${ticker}_${cusip}_distribution.json`;
  fs.writeFileSync(rawPath, body);
  inventory.push({
    ticker, cusip, url, raw_file: rawPath.replace(`${OUT}/`, ''),
    bytes: Buffer.byteLength(body), sha256: crypto.createHash('sha256').update(body).digest('hex'),
  });
  if (!Array.isArray(json.distributions)) throw new Error(`${ticker}: distributions array missing`);
  for (const d of json.distributions) {
    rows.push({
      ticker,
      ex_date: d.exDate ?? '',
      record_date: d.recordDate ?? '',
      pay_date: d.payDate ?? '',
      action_type: 'cash_dividend',
      cash_amount_issuer_reported: d.distributionAmountPerUnit ?? '',
      ordinary_income: d.ordinaryIncomeDistribution ?? '',
      short_term_cap_gain: d.shortTermCapitalGainsDistribution ?? '',
      long_term_cap_gain: d.longTermCapitalGainsDistribution ?? '',
      return_of_capital: d.returnOfCapitalDistribution ?? '',
      issuer: 'Invesco',
      issuer_cusip: cusip,
      source: 'issuer_official_invesco_dng_api',
      source_snapshot_date: '2026-08-19',
      amount_basis: 'issuer_reported_to_be_resolved_by_split_crosscheck',
    });
  }
}
rows.sort((a,b) => a.ticker.localeCompare(b.ticker) || String(a.ex_date).localeCompare(String(b.ex_date)));
function csvCell(v) {
  const s = v == null ? '' : String(v);
  return /[",\n]/.test(s) ? `"${s.replaceAll('"','""')}"` : s;
}
function writeCsv(path, data) {
  const fields = Object.keys(data[0]);
  const text = [fields.join(','), ...data.map(r => fields.map(f => csvCell(r[f])).join(','))].join('\n') + '\n';
  fs.writeFileSync(path, text);
}
writeCsv(`${OUT}/invesco_issuer_cash_distributions.csv`, rows);
writeCsv(`${OUT}/invesco_fetch_inventory.csv`, inventory);
fs.writeFileSync(`${OUT}/invesco_summary.json`, JSON.stringify({ rows: rows.length, counts: Object.fromEntries(Object.keys(funds).map(t => [t, rows.filter(r=>r.ticker===t).length])), inventory }, null, 2));
if (Object.keys(funds).some(t => rows.filter(r => r.ticker === t).length < 20)) throw new Error('insufficient Invesco distribution history');
console.log(JSON.stringify({ rows: rows.length, counts: Object.fromEntries(Object.keys(funds).map(t => [t, rows.filter(r=>r.ticker===t).length])) }, null, 2));
