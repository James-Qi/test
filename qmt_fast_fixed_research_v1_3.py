#coding:gbk
# ASCII-only QMT fast fixed-parameter research version (balanced logging v1_3).
# Single path, no real backtest orders, no actual sync, internal virtual fill.
# Goal: keep speed, but do not become a black box.

# -----------------------------------------------------------------------------
# Strategy overview (for future maintenance)
# 1) This script is designed for QMT callback lifecycle: init -> handlebar -> stop.
# 2) No real order is sent. Positions are rebalanced internally by virtual fills.
# 3) Pair spread is defined as log(A) - log(B).
# 4) Each AM/PM session has two phases:
#    - Anchor window: collect spread samples and estimate mu/sigma.
#    - Trade window: generate entry/exit/stop signals from deviation bands.
# 5) Rebalance target profiles:
#    - NEUTRAL: 50% A + 50% B
#    - A_HEAVY: 100% A
#    - B_HEAVY: 100% B
# 6) Extensive logs are intentionally kept to avoid black-box behavior.
# -----------------------------------------------------------------------------

import math
import datetime as dt

A_CODE = '511090.SH'
B_CODE = '511130.SH'

ENTRY_K = 1.60
EXIT_K  = 0.40
STOP_K  = 6.00

INITIAL_CAPITAL = 500000.0
LOT_SIZE = 100
FEE_RATE = 0.0

AM_ANCHOR_START = 94000
AM_ANCHOR_END   = 100000
AM_TRADE_START  = 100000
AM_TRADE_END    = 113000

PM_ANCHOR_START = 133000
PM_ANCHOR_END   = 140000
PM_TRADE_START  = 140000
PM_TRADE_END    = 145700

MIN_ANCHOR_N = 20
MIN_SIGMA = 1e-8

# balanced logs
LOG_FIRST_BARS = 5
LOG_PROGRESS_EVERY = 300
LOG_ANCHOR_EVERY = 60
LOG_SIGNAL_EVERY = 60
LOG_MISS_FIRST = 20
LOG_MISS_EVERY = 100
LOG_FETCH_FAIL_FIRST = 5

PROFILE_WEIGHTS = {
    'NEUTRAL': (0.50, 0.50),
    'A_HEAVY': (1.00, 0.00),
    'B_HEAVY': (0.00, 1.00),
}


# Convert input to float safely; return default on any parse error.
def safe_num(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


# Normalize symbol to QMT-like suffix format (e.g. 511090 -> 511090.SH).
def norm_code(code):
    s = str(code).upper().strip()
    if s.endswith('.SH') or s.endswith('.SZ'):
        return s
    if len(s) == 6:
        if s[0] in ('5', '6', '9'):
            return s + '.SH'
        return s + '.SZ'
    return s


# Parse bar timetag from multiple possible formats:
# - YYYYmmddHHMMSS integer-like value
# - ns/us/ms/s epoch-like value
def parse_bar_datetime(raw_timetag):
    try:
        x = int(raw_timetag)
    except Exception:
        return None
    sx = str(x)
    if len(sx) == 14 and sx.startswith('20'):
        try:
            return dt.datetime.strptime(sx, '%Y%m%d%H%M%S')
        except Exception:
            pass
    try:
        if x > 10 ** 18:
            ts = x / 1e9
        elif x > 10 ** 15:
            ts = x / 1e6
        elif x > 10 ** 12:
            ts = x / 1e3
        else:
            ts = float(x)
        return dt.datetime.fromtimestamp(ts)
    except Exception:
        return None


def ymd_hms(d):
    if d is None:
        return None, None
    return d.year * 10000 + d.month * 100 + d.day, d.hour * 10000 + d.minute * 100 + d.second


# Determine whether current bar belongs to AM / PM trading session scope.
def which_session(hms):
    if hms is None:
        return None
    if AM_ANCHOR_START <= hms < AM_TRADE_END:
        return 'AM'
    if PM_ANCHOR_START <= hms < PM_TRADE_END:
        return 'PM'
    return None


def is_anchor_time(sess, hms):
    if sess == 'AM':
        return AM_ANCHOR_START <= hms < AM_ANCHOR_END
    if sess == 'PM':
        return PM_ANCHOR_START <= hms < PM_ANCHOR_END
    return False


def is_trade_time(sess, hms):
    if sess == 'AM':
        return AM_TRADE_START <= hms < AM_TRADE_END
    if sess == 'PM':
        return PM_TRADE_START <= hms < PM_TRADE_END
    return False


# Sample standard deviation (n-1 denominator).
def stddev(xs):
    n = len(xs)
    if n <= 1:
        return 0.0
    m = sum(xs) / float(n)
    v = sum((x - m) * (x - m) for x in xs) / float(n - 1)
    if v < 0:
        v = 0.0
    return math.sqrt(v)


# Extract a symbol quote record from nested/heterogeneous API payloads.
# This function is intentionally defensive because QMT return shapes can vary.
def extract_record(obj, code):
    code = norm_code(code)
    base = code.split('.')[0]

    if obj is None:
        return None

    try:
        if hasattr(obj, 'index') and hasattr(obj, 'loc'):
            idx = obj.index
            if code in idx:
                return extract_record(obj.loc[code], code)
            if base in idx:
                return extract_record(obj.loc[base], code)
        if hasattr(obj, 'iloc'):
            try:
                if len(obj) > 0:
                    return extract_record(obj.iloc[-1], code)
            except Exception:
                pass
    except Exception:
        pass

    try:
        if hasattr(obj, 'to_dict'):
            d = obj.to_dict()
            if d is not obj:
                return extract_record(d, code)
    except Exception:
        pass

    if isinstance(obj, dict):
        if 'quoter' in obj:
            return extract_record(obj['quoter'], code)
        if ('lastPrice' in obj) or ('askPrice' in obj) or ('bidPrice' in obj) or ('close' in obj):
            return obj
        if code in obj:
            return extract_record(obj[code], code)
        if base in obj:
            return extract_record(obj[base], code)
        if len(obj) == 1:
            try:
                return extract_record(list(obj.values())[0], code)
            except Exception:
                return None

    if isinstance(obj, (list, tuple)):
        if len(obj) == 0:
            return None
        return extract_record(obj[-1], code)

    return None


def _first_price(x):
    try:
        if isinstance(x, (list, tuple)):
            if len(x) > 0 and x[0] is not None:
                return float(x[0])
            return None
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


# Best-effort mid price: (ask1+bid1)/2 -> lastPrice -> close.
def get_mid(rec):
    if not rec:
        return None
    a1 = _first_price(rec.get('askPrice'))
    b1 = _first_price(rec.get('bidPrice'))
    if a1 is not None and b1 is not None and a1 > 0 and b1 > 0:
        return 0.5 * (a1 + b1)
    lp = _first_price(rec.get('lastPrice'))
    if lp is not None and lp > 0:
        return lp
    cp = _first_price(rec.get('close'))
    if cp is not None and cp > 0:
        return cp
    return None


# Try one market-data API mode and return pair prices for A/B.
def try_fetch_pair(ContextInfo, end_str, mode):
    api = mode[0]
    fields = mode[1]
    if api == 'ex':
        res = ContextInfo.get_market_data_ex(
            fields=fields,
            stock_code=[A_CODE, B_CODE],
            period='tick',
            start_time='',
            end_time=end_str,
            count=1,
            dividend_type='none',
            fill_data=False,
            subscribe=False,
        )
    else:
        res = ContextInfo.get_market_data(
            fields, [A_CODE, B_CODE],
            start_time='',
            end_time=end_str,
            skip_paused=True,
            period='tick',
            dividend_type='none',
            count=1
        )
    ra = extract_record(res, A_CODE)
    rb = extract_record(res, B_CODE)
    pa = get_mid(ra)
    pb = get_mid(rb)
    if pa is None or pb is None or pa <= 0 or pb <= 0:
        return None, None
    return pa, pb


def one_time_single_probe(ContextInfo, end_str):
    # Only for diagnostics on early misses. Cost is acceptable because it runs only once.
    modes = [
        ('ex', ['lastPrice', 'askPrice', 'bidPrice']),
        ('ex', ['quoter']),
        ('old', ['quoter']),
    ]
    msgs = []
    for code in [A_CODE, B_CODE]:
        ok = False
        for m in modes:
            try:
                if m[0] == 'ex':
                    res = ContextInfo.get_market_data_ex(
                        fields=m[1],
                        stock_code=[code],
                        period='tick',
                        start_time='',
                        end_time=end_str,
                        count=1,
                        dividend_type='none',
                        fill_data=False,
                        subscribe=False,
                    )
                else:
                    res = ContextInfo.get_market_data(
                        m[1], [code],
                        start_time='',
                        end_time=end_str,
                        skip_paused=True,
                        period='tick',
                        dividend_type='none',
                        count=1
                    )
                rec = extract_record(res, code)
                px = get_mid(rec)
                if px is not None and px > 0:
                    msgs.append('%s:OK api=%s fields=%s px=%.6f' % (code, m[0], str(m[1]), px))
                    ok = True
                    break
            except Exception:
                pass
        if not ok:
            msgs.append('%s:FAIL' % code)
    return ' | '.join(msgs)


# Fetch A/B prices with fallback modes and sticky preferred mode cache.
def fetch_pair_snapshot(ContextInfo, cur_dt, st):
    if cur_dt is None:
        return None, None
    end_str = cur_dt.strftime('%Y%m%d%H%M%S')

    saved = st.get('fetch_mode')
    if saved is not None:
        try:
            pa, pb = try_fetch_pair(ContextInfo, end_str, saved)
            if pa is not None and pb is not None:
                return pa, pb
        except Exception:
            st['fetch_mode'] = None

    modes = [
        ('ex', ['lastPrice', 'askPrice', 'bidPrice']),
        ('ex', ['quoter']),
        ('old', ['quoter']),
    ]
    for m in modes:
        try:
            pa, pb = try_fetch_pair(ContextInfo, end_str, m)
            if pa is not None and pb is not None:
                st['fetch_mode'] = m
                print('[FETCH_MODE] api=%s fields=%s' % (m[0], str(m[1])))
                return pa, pb
        except Exception:
            pass

    st['fetch_fail_count'] += 1
    if st['fetch_fail_count'] <= LOG_FETCH_FAIL_FIRST:
        print('[FETCH_FAIL] dt=%s end=%s saved_mode=%s' % (str(cur_dt), end_str, str(saved)))
    if (not st['single_probe_done']) and st['fetch_fail_count'] <= 3:
        st['single_probe_done'] = True
        print('[SINGLE_PROBE] %s' % one_time_single_probe(ContextInfo, end_str))

    return None, None


def mark_equity(st, a_px, b_px):
    return st['cash'] + st['qty_a'] * a_px + st['qty_b'] * b_px


def floor_lot_qty(value_amount, price):
    if price is None or price <= 0:
        return 0
    raw = int(value_amount / price)
    if raw < LOT_SIZE:
        return 0
    return (raw // LOT_SIZE) * LOT_SIZE


# Virtual rebalance engine:
# - compute target quantities from total equity
# - apply lot rounding and fees
# - update state and counters
def apply_virtual_profile(st, profile, a_px, b_px, reason, ymd, sess, hms):
    if profile == st['actual_profile']:
        return

    total = mark_equity(st, a_px, b_px)
    wa, wb = PROFILE_WEIGHTS[profile]

    target_a = floor_lot_qty(total * wa, a_px)
    target_b = floor_lot_qty(total * wb, b_px)

    old_a = st['qty_a']
    old_b = st['qty_b']

    delta_a = target_a - old_a
    delta_b = target_b - old_b

    trade_turnover = abs(delta_a) * a_px + abs(delta_b) * b_px
    fee = trade_turnover * FEE_RATE

    new_cash = total - target_a * a_px - target_b * b_px - fee

    st['qty_a'] = target_a
    st['qty_b'] = target_b
    st['cash'] = new_cash
    st['fees'] += fee
    st['turnover'] += trade_turnover
    st['rebalances'] += 1
    st['actual_profile'] = profile

    if reason.startswith('enter'):
        st['entries'] += 1
    elif reason.startswith('exit'):
        st['exits'] += 1
    elif reason.startswith('stop'):
        st['stops'] += 1

    print('[TRADE] date=%s session=%s time=%s reason=%s profile=%s qty_a=%s qty_b=%s equity=%.2f cash=%.2f turnover=%.2f' % (
        str(ymd), str(sess), str(hms), reason, profile, st['qty_a'], st['qty_b'],
        mark_equity(st, a_px, b_px), st['cash'], st['turnover']))


# Reset anchor statistics and intraday regime state for a new session.
def reset_session_state(st, ymd, sess):
    st['cur_ymd'] = ymd
    st['cur_session'] = sess
    st['anchor_spreads'] = []
    st['mu'] = None
    st['sigma'] = None
    st['entry_band'] = None
    st['exit_band'] = None
    st['stop_band'] = None
    st['regime'] = 'NEUTRAL'
    st['signal_count'] = 0
    print('[SESSION_RESET] date=%s session=%s' % (str(ymd), str(sess)))


def print_day_summary(st, a_px, b_px, reason):
    if st.get('day_ymd') is None:
        return
    eq = mark_equity(st, a_px, b_px) if (a_px is not None and b_px is not None) else st.get('last_equity', INITIAL_CAPITAL)
    day_pnl = eq - st['day_start_equity']
    print('[DAY_SUMMARY] date=%s reason=%s bars=%s trade_bars=%s out=%s miss=%s zero=%s entries=%s exits=%s stops=%s rebalances=%s equity=%.2f day_pnl=%.2f fees=%.2f turnover=%.2f profile=%s qty_a=%s qty_b=%s cash=%.2f' % (
        str(st['day_ymd']), reason, st['day_bars'], st['day_trade_bars'], st['day_out'],
        st['miss'], st['zero'], st['entries'], st['exits'], st['stops'], st['rebalances'],
        eq, day_pnl, st['fees'], st['turnover'], st['actual_profile'], st['qty_a'], st['qty_b'], st['cash']))


# Reset daily counters and snapshot day-start equity baseline.
def start_new_day(st, ymd):
    st['day_ymd'] = ymd
    st['day_bars'] = 0
    st['day_trade_bars'] = 0
    st['day_out'] = 0
    st['entries'] = 0
    st['exits'] = 0
    st['stops'] = 0
    st['rebalances'] = 0
    st['fees'] = 0.0
    st['turnover'] = 0.0
    last_a = safe_num(st.get('last_a'), 0.0)
    last_b = safe_num(st.get('last_b'), 0.0)
    st['day_start_equity'] = st['cash'] + st['qty_a'] * last_a + st['qty_b'] * last_b
    print('[DAY_START] date=%s start_equity=%.2f' % (str(ymd), st['day_start_equity']))


# QMT lifecycle entry: initialize universe, strategy state, and runtime flags.
def init(ContextInfo):
    try:
        ContextInfo.set_universe([A_CODE, B_CODE])
    except Exception:
        pass
    try:
        ContextInfo.data_info_level = 0
    except Exception:
        pass

    st = {}
    st['bars'] = 0
    st['out'] = 0
    st['miss'] = 0
    st['zero'] = 0

    st['cash'] = INITIAL_CAPITAL
    st['qty_a'] = 0
    st['qty_b'] = 0
    st['actual_profile'] = 'EMPTY'

    st['cur_ymd'] = None
    st['cur_session'] = None
    st['anchor_spreads'] = []
    st['mu'] = None
    st['sigma'] = None
    st['entry_band'] = None
    st['exit_band'] = None
    st['stop_band'] = None
    st['regime'] = 'NEUTRAL'
    st['signal_count'] = 0

    st['day_ymd'] = None
    st['day_bars'] = 0
    st['day_trade_bars'] = 0
    st['day_out'] = 0
    st['entries'] = 0
    st['exits'] = 0
    st['stops'] = 0
    st['rebalances'] = 0
    st['fees'] = 0.0
    st['turnover'] = 0.0
    st['day_start_equity'] = INITIAL_CAPITAL

    st['fetch_mode'] = None
    st['fetch_fail_count'] = 0
    st['single_probe_done'] = False
    st['last_a'] = None
    st['last_b'] = None
    st['last_equity'] = INITIAL_CAPITAL
    st['first_valid_logged'] = False

    ContextInfo.user_data = st

    print('[INIT] FAST_FIXED_RESEARCH_V1_3')
    print('[INIT] pair=%s vs %s' % (A_CODE, B_CODE))
    print('[INIT] ENTRY=%.2f EXIT=%.2f STOP=%.2f capital=%.2f' % (ENTRY_K, EXIT_K, STOP_K, INITIAL_CAPITAL))
    print('[INIT] main_chart=%s period=%s' % (norm_code(getattr(ContextInfo, 'stockcode', '')), getattr(ContextInfo, 'period', '')))
    print('[INIT] mode=virtual_only no_real_orders no_actual_sync single_path balanced_log_with_miss_debug')


# QMT lifecycle callback per bar/tick:
# - manage day/session transitions
# - fetch prices
# - collect anchor samples
# - generate signals and rebalance virtually
def handlebar(ContextInfo):
    st = ContextInfo.user_data
    st['bars'] += 1
    bar = st['bars']

    try:
        raw_timetag = ContextInfo.get_bar_timetag(ContextInfo.barpos)
    except Exception:
        raw_timetag = None

    cur_dt = parse_bar_datetime(raw_timetag)
    ymd, hms = ymd_hms(cur_dt)
    sess = which_session(hms)

    if st['day_ymd'] is None and ymd is not None:
        start_new_day(st, ymd)

    if st['day_ymd'] is not None and ymd is not None and ymd != st['day_ymd']:
        print_day_summary(st, st.get('last_a'), st.get('last_b'), 'day_change')
        start_new_day(st, ymd)
        st['actual_profile'] = 'EMPTY'
        st['qty_a'] = 0
        st['qty_b'] = 0
        st['cash'] = st['last_equity']
        st['cur_ymd'] = None
        st['cur_session'] = None
        st['regime'] = 'NEUTRAL'

    if st['day_ymd'] is not None:
        st['day_bars'] += 1

    if bar <= LOG_FIRST_BARS:
        print('[BAR] idx=%s dt=%s session=%s' % (bar, str(cur_dt), str(sess)))

    # Heartbeat before fetch, even if later miss continues.
    if LOG_PROGRESS_EVERY > 0 and (bar % LOG_PROGRESS_EVERY) == 0:
        print('[PROGRESS] idx=%s dt=%s session=%s miss=%s zero=%s out=%s profile=%s' % (
            bar, str(cur_dt), str(sess), st['miss'], st['zero'], st['out'], st['actual_profile']))

    if sess is None:
        st['out'] += 1
        if st['day_ymd'] is not None:
            st['day_out'] += 1
        return

    a_px, b_px = fetch_pair_snapshot(ContextInfo, cur_dt, st)
    if a_px is None or b_px is None:
        st['miss'] += 1
        if st['miss'] <= LOG_MISS_FIRST or (LOG_MISS_EVERY > 0 and (st['miss'] % LOG_MISS_EVERY) == 0):
            print('[MISS] idx=%s dt=%s session=%s miss=%s fetch_mode=%s' % (
                bar, str(cur_dt), str(sess), st['miss'], str(st.get('fetch_mode'))))
        return

    if a_px <= 0 or b_px <= 0:
        st['zero'] += 1
        if st['zero'] <= 3:
            print('[ZERO] idx=%s dt=%s a=%s b=%s' % (bar, str(cur_dt), str(a_px), str(b_px)))
        return

    st['last_a'] = a_px
    st['last_b'] = b_px
    st['last_equity'] = mark_equity(st, a_px, b_px)

    if not st['first_valid_logged']:
        st['first_valid_logged'] = True
        print('[FIRST_VALID] idx=%s dt=%s session=%s a=%.6f b=%.6f equity=%.2f' % (
            bar, str(cur_dt), str(sess), a_px, b_px, st['last_equity']))

    if st['cur_ymd'] != ymd or st['cur_session'] != sess:
        reset_session_state(st, ymd, sess)
        apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'session_reset', ymd, sess, hms)
        st['last_equity'] = mark_equity(st, a_px, b_px)

    spread = math.log(a_px) - math.log(b_px)

    if is_anchor_time(sess, hms):
        st['anchor_spreads'].append(spread)
        n = len(st['anchor_spreads'])
        if n <= 5 or (LOG_ANCHOR_EVERY > 0 and (n % LOG_ANCHOR_EVERY) == 0):
            print('[ANCHOR_ACC] ymd=%s session=%s hms=%s n=%s spr=%.10f' % (
                ymd, sess, hms, n, spread))
        return

    if not is_trade_time(sess, hms):
        return

    if st['day_ymd'] is not None:
        st['day_trade_bars'] += 1

    n = len(st['anchor_spreads'])
    if n < MIN_ANCHOR_N:
        return

    if st['mu'] is None:
        mu = sum(st['anchor_spreads']) / float(n)
        sigma = stddev(st['anchor_spreads'])
        st['mu'] = mu
        st['sigma'] = sigma
        if sigma > MIN_SIGMA:
            st['entry_band'] = ENTRY_K * sigma
            st['exit_band'] = EXIT_K * sigma
            st['stop_band'] = STOP_K * sigma
            print('[ANCHOR_READY] ymd=%s session=%s n=%s mu=%.10f sigma=%.10f entry=%.10f exit=%.10f stop=%.10f' % (
                ymd, sess, n, mu, sigma, st['entry_band'], st['exit_band'], st['stop_band']))
        else:
            print('[ANCHOR_FAIL] ymd=%s session=%s n=%s sigma=%.12f too_small' % (ymd, sess, n, sigma))
            return

    if st['sigma'] is None or st['sigma'] <= MIN_SIGMA:
        return

    dev = spread - st['mu']
    regime = st['regime']

    st['signal_count'] += 1
    if st['signal_count'] <= 3 or (LOG_SIGNAL_EVERY > 0 and (st['signal_count'] % LOG_SIGNAL_EVERY) == 0):
        print('[SIGNAL] ymd=%s session=%s hms=%s spr=%.10f dev=%.10f z=%.6f profile=%s a=%.6f b=%.6f' % (
            ymd, sess, hms, spread, dev, dev / st['sigma'], st['actual_profile'], a_px, b_px))

    if regime == 'NEUTRAL':
        if dev >= st['entry_band']:
            st['regime'] = 'A_RICH'
            print('[ENTER] date=%s session=%s time=%s side=A_RICH -> target=B_HEAVY dev=%.10f z=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'B_HEAVY', a_px, b_px, 'enter_A_rich', ymd, sess, hms)
        elif dev <= -st['entry_band']:
            st['regime'] = 'B_RICH'
            print('[ENTER] date=%s session=%s time=%s side=B_RICH -> target=A_HEAVY dev=%.10f z=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'A_HEAVY', a_px, b_px, 'enter_B_rich', ymd, sess, hms)

    elif regime == 'A_RICH':
        if dev <= st['exit_band']:
            st['regime'] = 'NEUTRAL'
            print('[EXIT] date=%s session=%s time=%s from=A_RICH dev=%.10f z=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'exit_A_rich', ymd, sess, hms)
        elif dev >= st['stop_band']:
            st['regime'] = 'NEUTRAL'
            print('[STOP] date=%s session=%s time=%s from=A_RICH dev=%.10f z=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'stop_A_rich', ymd, sess, hms)

    elif regime == 'B_RICH':
        if dev >= -st['exit_band']:
            st['regime'] = 'NEUTRAL'
            print('[EXIT] date=%s session=%s time=%s from=B_RICH dev=%.10f z=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'exit_B_rich', ymd, sess, hms)
        elif dev <= -st['stop_band']:
            st['regime'] = 'NEUTRAL'
            print('[STOP] date=%s session=%s time=%s from=B_RICH dev=%.10f z=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'stop_B_rich', ymd, sess, hms)

    st['last_equity'] = mark_equity(st, a_px, b_px)


# QMT lifecycle exit: print final summaries.
def stop(ContextInfo):
    st = ContextInfo.user_data
    print_day_summary(st, st.get('last_a'), st.get('last_b'), 'stop')
    print('[BACKTEST_SUMMARY] reason=stop bars=%s miss=%s zero=%s out=%s final_equity=%.2f profile=%s qty_a=%s qty_b=%s cash=%.2f' % (
        st.get('bars', 0), st.get('miss', 0), st.get('zero', 0), st.get('out', 0),
        st.get('last_equity', INITIAL_CAPITAL), st.get('actual_profile', 'NA'),
        st.get('qty_a', 0), st.get('qty_b', 0), st.get('cash', INITIAL_CAPITAL)))
