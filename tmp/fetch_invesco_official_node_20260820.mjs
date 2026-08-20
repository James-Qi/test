import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';

const OUT = path.resolve('artifact');
fs.mkdirSync(path.join(OUT, 'raw'), { recursive: true });
const funds = { RSP: '46137V357', QQQ: '46090E103' };
const headers = {
  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  'Accept': 'application/json,*/*',
  'Referer': 'https://www.invesco.com/',
};
const log = [];
const summary = [];
const normalized = [];

function sha256(buf) { return crypto.createHash('sha256').update(buf).digest('hex'); }
async function get(url) {
  let last = '';
  for (let attempt = 1; attempt <= 6; attempt++) {
    const res = await fetch(url, { headers });
    const body = Buffer.from(await res.arrayBuffer());
    log.push({ url, attempt, status: res.status, ok: res.ok, bytes: body.length, sha256: sha256(body), body_prefix: body.toString('utf8', 0, 200) });
    if (res.ok) return body;
    last = body.toString('utf8', 0, 500);
    if (res.status >= 500) await new Promise(r => setTimeout(r, 250 * attempt));
    else throw new Error(`HTTP ${res.status}: ${last}`);
  }
  throw new Error(last);
}

for (const [ticker, cusip] of Object.entries(funds)) {
  const base = `https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/${cusip}`;
  const distUrl = `${base}/distribution?idType=cusip&productType=ETF&loadType=initial`;
  const navUrl = `${base}/navs?idType=cusip&productType=ETF`;
  const detailsUrl = `${base}?idType=cusip&productType=ETF&expand=nav&variationType=fundDetails`;
  const [distBody, navBody, detailsBody] = await Promise.all([get(distUrl), get(navUrl), get(detailsUrl)]);
  const distPath = path.join(OUT, 'raw', `${ticker}_distribution.json`);
  const navPath = path.join(OUT, 'raw', `${ticker}_navs.json`);
  const detailsPath = path.join(OUT, 'raw', `${ticker}_fundDetails.json`);
  fs.writeFileSync(distPath, distBody); fs.writeFileSync(navPath, navBody); fs.writeFileSync(detailsPath, detailsBody);
  const obj = JSON.parse(distBody.toString('utf8'));
  const items = Array.isArray(obj.distributions) ? obj.distributions : [];
  for (const it of items) {
    const amount = Number(String(it.distributionAmountPerUnit ?? '').replace(/[$,%\s]/g, ''));
    if (!it.exDate || !Number.isFinite(amount)) continue;
    normalized.push({
      ticker, issuer: 'Invesco', ex_date: it.exDate, record_date: it.recordDate ?? '', pay_date: it.payDate ?? '',
      action_type: 'cash_dividend', cash_amount_issuer_reported: amount,
      ordinary_income: it.ordinaryIncomeDistribution ?? '', short_term_capital_gain: it.shortTermCapitalGainsDistribution ?? '',
      long_term_capital_gain: it.longTermCapitalGainsDistribution ?? '', return_of_capital: it.returnOfCapitalDistribution ?? '',
      source: 'issuer_official_invesco_dng_api', source_url: distUrl, source_snapshot_date: '2026-08-20',
      source_file: `raw/${ticker}_distribution.json`, source_file_sha256: sha256(distBody),
    });
  }
  summary.push({ ticker, cusip, distribution_rows: items.length, min_ex_date: items.at(-1)?.exDate ?? '', max_ex_date: items[0]?.exDate ?? '', dist_sha256: sha256(distBody), nav_sha256: sha256(navBody), details_sha256: sha256(detailsBody) });
}

function csv(rows) {
  if (!rows.length) return '';
  const fields = [...new Set(rows.flatMap(r => Object.keys(r)))];
  const esc = v => { const s = String(v ?? ''); return /[",\n]/.test(s) ? `"${s.replaceAll('"','""')}"` : s; };
  return fields.join(',') + '\n' + rows.map(r => fields.map(f => esc(r[f])).join(',')).join('\n') + '\n';
}
fs.writeFileSync(path.join(OUT, 'invesco_official_distributions.csv'), csv(normalized));
fs.writeFileSync(path.join(OUT, 'summary.csv'), csv(summary));
fs.writeFileSync(path.join(OUT, 'fetch_log.json'), JSON.stringify(log, null, 2));
fs.writeFileSync(path.join(OUT, 'summary.json'), JSON.stringify({ generated_at_utc: new Date().toISOString(), funds: summary, normalized_rows: normalized.length }, null, 2));
console.log(JSON.stringify({ summary, normalized_rows: normalized.length }, null, 2));
if (normalized.length < 100) throw new Error(`unexpectedly low distribution rows: ${normalized.length}`);
