#coding:utf-8
# QMT 快速固定参数研究版本（均衡日志 v1_3）。
# 单路径：不下真实回测订单，不做真实同步，内部虚拟成交。
# 目标：保持速度，同时避免黑箱化。

# -----------------------------------------------------------------------------
# 策略总览（便于后续维护）
# 1）脚本遵循 QMT 回调生命周期：init -> handlebar -> stop。
# 2）不发送真实订单，仓位通过内部虚拟成交进行再平衡。
# 3）配对价差定义为 log(A) - log(B)。
# 4）每个上午/下午时段分为两个阶段：
#    - 锚定窗口：采集价差样本并估计 mu/sigma。
#    - 交易窗口：基于偏离阈值产生入场/退出/止损信号。
# 5）再平衡目标组合：
#    - NEUTRAL：50% A + 50% B
#    - A_HEAVY：100% A
#    - B_HEAVY：100% B
# 6）保留大量日志，刻意避免黑箱行为。
# -----------------------------------------------------------------------------

import math
import datetime as dt

A_CODE = '511090.SH'  # 标的A代码
B_CODE = '511130.SH'  # 标的B代码

ENTRY_K = 1.60  # 入场阈值倍数（相对sigma）
EXIT_K  = 0.40  # 退出阈值倍数（相对sigma）
STOP_K  = 6.00  # 止损阈值倍数（相对sigma）

INITIAL_CAPITAL = 500000.0  # 初始资金
LOT_SIZE = 100  # 最小交易单位（整手）
FEE_RATE = 0.0  # 手续费率

AM_ANCHOR_START = 94000  # 上午锚定开始时刻
AM_ANCHOR_END   = 100000  # 上午锚定结束时刻
AM_TRADE_START  = 100000  # 上午交易开始时刻
AM_TRADE_END    = 113000  # 上午交易结束时刻

PM_ANCHOR_START = 133000  # 下午锚定开始时刻
PM_ANCHOR_END   = 140000  # 下午锚定结束时刻
PM_TRADE_START  = 140000  # 下午交易开始时刻
PM_TRADE_END    = 145700  # 下午交易结束时刻

MIN_ANCHOR_N = 20  # 最小锚定样本数
MIN_SIGMA = 1e-8  # 最小波动阈值（避免除零）

# 均衡日志
LOG_FIRST_BARS = 5  # 前N根输出详细bar日志
LOG_PROGRESS_EVERY = 300  # 每N根输出进度日志
LOG_ANCHOR_EVERY = 60  # 锚定阶段每N条输出一次
LOG_SIGNAL_EVERY = 60  # 信号阶段每N条输出一次
LOG_MISS_FIRST = 20  # 前N次拉取缺失都打印
LOG_MISS_EVERY = 100  # 之后每N次缺失打印
LOG_FETCH_FAIL_FIRST = 5  # 前N次拉取失败打印

PROFILE_WEIGHTS = {
    'NEUTRAL': (0.50, 0.50),
    'A_HEAVY': (1.00, 0.00),
    'B_HEAVY': (0.00, 1.00),
}


# 行情拉取回退模式（常量化，避免在高频路径重复创建列表对象）
FETCH_MODES = (
    ('ex', ['lastPrice', 'askPrice', 'bidPrice']),
    ('ex', ['quoter']),
    ('old', ['quoter']),
)


# 安全转为浮点数；任意解析失败时返回默认值。
def safe_num(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


# 规范化证券代码到 QMT 后缀格式（如 511090 -> 511090.SH）。
def norm_code(code):
    s = str(code).upper().strip()
    if s.endswith('.SH') or s.endswith('.SZ'):
        return s
    if len(s) == 6:
        if s[0] in ('5', '6', '9'):
            return s + '.SH'
        return s + '.SZ'
    return s


# 解析 bar 时间戳，兼容多种格式：
# - YYYYmmddHHMMSS 形式整数
# - ns/us/ms/s 级别时间戳
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


# 将 datetime 拆分为 yyyymmdd 与 hhmmss 整数。
def ymd_hms(d):
    if d is None:
        return None, None
    return d.year * 10000 + d.month * 100 + d.day, d.hour * 10000 + d.minute * 100 + d.second


# 判断当前 bar 是否位于上午/下午会话区间。
# 根据时分秒整数判断所属会话（AM/PM/None）。
def which_session(hms):
    if hms is None:
        return None
    if AM_ANCHOR_START <= hms < AM_TRADE_END:
        return 'AM'
    if PM_ANCHOR_START <= hms < PM_TRADE_END:
        return 'PM'
    return None


# 判断当前时刻是否位于对应会话的锚定窗口。
def is_anchor_time(sess, hms):
    if sess == 'AM':
        return AM_ANCHOR_START <= hms < AM_ANCHOR_END
    if sess == 'PM':
        return PM_ANCHOR_START <= hms < PM_ANCHOR_END
    return False


# 判断当前时刻是否位于对应会话的交易窗口。
def is_trade_time(sess, hms):
    if sess == 'AM':
        return AM_TRADE_START <= hms < AM_TRADE_END
    if sess == 'PM':
        return PM_TRADE_START <= hms < PM_TRADE_END
    return False


# 样本标准差（分母 n-1）。
def stddev(xs):
    n = len(xs)
    if n <= 1:
        return 0.0
    m = sum(xs) / float(n)
    v = sum((x - m) * (x - m) for x in xs) / float(n - 1)
    if v < 0:
        v = 0.0
    return math.sqrt(v)


# 从嵌套/异构 API 返回结构中提取指定标的行情记录。
# 该函数采用防御式写法，因为 QMT 返回结构可能变化。
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


# 从标量或数组中提取首个可用价格。
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


# 尽力取中间价：(ask1+bid1)/2 -> lastPrice -> close。
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


# 尝试一种行情接口模式，返回 A/B 两腿价格。
def try_fetch_pair(ContextInfo, end_str, mode):
    """
    参数说明：
    - ContextInfo: QMT上下文对象
    - end_str: 结束时间字符串，格式 YYYYmmddHHMMSS
    - mode: 拉取模式元组 (api_type, fields)
    """
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
    """单次诊断探测：用于早期缺失排查。"""
    # 仅用于早期缺失诊断；只运行一次，开销可接受。
    modes = FETCH_MODES
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
                    msgs.append('%s:成功 接口=%s fields=%s 价格=%.6f' % (code, m[0], str(m[1]), px))
                    ok = True
                    break
            except Exception:
                pass
        if not ok:
            msgs.append('%s:失败' % code)
    return ' | '.join(msgs)


# 使用回退模式与粘性缓存模式拉取 A/B 价格。
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
            st['fetch_mode'] = None  # 当前可用拉取模式缓存

    modes = FETCH_MODES
    for m in modes:
        try:
            pa, pb = try_fetch_pair(ContextInfo, end_str, m)
            if pa is not None and pb is not None:
                st['fetch_mode'] = m
                print('[拉取模式] 接口=%s fields=%s' % (m[0], str(m[1])))
                return pa, pb
        except Exception:
            pass

    st['fetch_fail_count'] += 1
    if st['fetch_fail_count'] <= LOG_FETCH_FAIL_FIRST:
        print('[拉取失败] 时间=%s 结束=%s 缓存模式=%s' % (str(cur_dt), end_str, str(saved)))
    if (not st['single_probe_done']) and st['fetch_fail_count'] <= 3:
        st['single_probe_done'] = True
        print('[单次探测] %s' % one_time_single_probe(ContextInfo, end_str))

    return None, None


# 按当前现金与持仓计算组合权益。
def mark_equity(st, a_px, b_px):
    return st['cash'] + st['qty_a'] * a_px + st['qty_b'] * b_px


# 根据目标金额与价格，按整手向下取整得到下单数量。
def floor_lot_qty(value_amount, price):
    if price is None or price <= 0:
        return 0
    raw = int(value_amount / price)
    if raw < LOT_SIZE:
        return 0
    return (raw // LOT_SIZE) * LOT_SIZE


# 虚拟再平衡引擎：
# - 根据总权益计算目标持仓
# - 应用整手取整与手续费
# - 更新状态与统计计数
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

    print('[交易] 日期=%s 会话=%s 时刻=%s 原因=%s 组合=%s A持仓=%s B持仓=%s 权益=%.2f 现金=%.2f 换手=%.2f' % (
        str(ymd), str(sess), str(hms), reason, profile, st['qty_a'], st['qty_b'],
        mark_equity(st, a_px, b_px), st['cash'], st['turnover']))


# 新会话开始时重置锚定统计与日内状态机。
def reset_session_state(st, ymd, sess):
    st['cur_ymd'] = ymd
    st['cur_session'] = sess
    st['anchor_spreads'] = []  # 锚定样本列表
    st['mu'] = None  # 锚定均值
    st['sigma'] = None  # 锚定波动
    st['entry_band'] = None  # 入场阈值
    st['exit_band'] = None  # 退出阈值
    st['stop_band'] = None  # 止损阈值
    st['regime'] = 'NEUTRAL'  # 状态机（NEUTRAL/A_RICH/B_RICH）
    st['signal_count'] = 0  # 信号计数
    print('[会话重置] 日期=%s 会话=%s' % (str(ymd), str(sess)))


# 打印日内汇总信息。
def print_day_summary(st, a_px, b_px, reason):
    if st.get('day_ymd') is None:
        return
    eq = mark_equity(st, a_px, b_px) if (a_px is not None and b_px is not None) else st.get('last_equity', INITIAL_CAPITAL)
    day_pnl = eq - st['day_start_equity']
    print('[日汇总] 日期=%s 原因=%s K线数=%s 交易K线数=%s 会话外=%s 缺失=%s 零值=%s 入场次数=%s 退出次数=%s 止损次数=%s 再平衡次数=%s 权益=%.2f 当日盈亏=%.2f 手续费=%.2f 换手=%.2f 组合=%s A持仓=%s B持仓=%s 现金=%.2f' % (
        str(st['day_ymd']), reason, st['day_bars'], st['day_trade_bars'], st['day_out'],
        st['miss'], st['zero'], st['entries'], st['exits'], st['stops'], st['rebalances'],
        eq, day_pnl, st['fees'], st['turnover'], st['actual_profile'], st['qty_a'], st['qty_b'], st['cash']))


# 重置日内计数，并记录当日初始权益基线。
def start_new_day(st, ymd):
    st['day_ymd'] = ymd
    st['day_bars'] = 0  # 当天bar数量
    st['day_trade_bars'] = 0  # 当天交易窗口bar数量
    st['day_out'] = 0  # 当天会话外bar数量
    st['entries'] = 0  # 入场次数
    st['exits'] = 0  # 退出次数
    st['stops'] = 0  # 止损次数
    st['rebalances'] = 0  # 再平衡次数
    st['fees'] = 0.0  # 累计手续费
    st['turnover'] = 0.0  # 累计换手额
    last_a = safe_num(st.get('last_a'), 0.0)
    last_b = safe_num(st.get('last_b'), 0.0)
    st['day_start_equity'] = st['cash'] + st['qty_a'] * last_a + st['qty_b'] * last_b
    print('[日开始] 日期=%s 起始权益=%.2f' % (str(ymd), st['day_start_equity']))


# 生命周期入口：初始化股票池、策略状态与运行标志。
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
    st['bars'] = 0  # 累计bar数量
    st['out'] = 0  # 会话外bar计数
    st['miss'] = 0  # 拉取缺失计数
    st['zero'] = 0  # 零价计数

    st['cash'] = INITIAL_CAPITAL  # 现金
    st['qty_a'] = 0  # A持仓数量
    st['qty_b'] = 0  # B持仓数量
    st['actual_profile'] = 'EMPTY'  # 当前真实（虚拟）组合

    st['cur_ymd'] = None  # 当前交易日
    st['cur_session'] = None  # 当前会话（AM/PM）
    st['anchor_spreads'] = []  # 锚定样本列表
    st['mu'] = None  # 锚定均值
    st['sigma'] = None  # 锚定波动
    st['entry_band'] = None  # 入场阈值
    st['exit_band'] = None  # 退出阈值
    st['stop_band'] = None  # 止损阈值
    st['regime'] = 'NEUTRAL'  # 状态机（NEUTRAL/A_RICH/B_RICH）
    st['signal_count'] = 0  # 信号计数

    st['day_ymd'] = None  # 当天日期
    st['day_bars'] = 0  # 当天bar数量
    st['day_trade_bars'] = 0  # 当天交易窗口bar数量
    st['day_out'] = 0  # 当天会话外bar数量
    st['entries'] = 0  # 入场次数
    st['exits'] = 0  # 退出次数
    st['stops'] = 0  # 止损次数
    st['rebalances'] = 0  # 再平衡次数
    st['fees'] = 0.0  # 累计手续费
    st['turnover'] = 0.0  # 累计换手额
    st['day_start_equity'] = INITIAL_CAPITAL  # 当天起始权益

    st['fetch_mode'] = None  # 当前可用拉取模式缓存
    st['fetch_fail_count'] = 0  # 拉取失败计数
    st['single_probe_done'] = False  # 单次探测是否执行
    st['last_a'] = None  # 最近A价格
    st['last_b'] = None  # 最近B价格
    st['last_equity'] = INITIAL_CAPITAL  # 最近权益
    st['first_valid_logged'] = False  # 首个有效价格是否已记录

    ContextInfo.user_data = st

    print('[初始化] 快速固定参数研究版_V1_3')
    print('[初始化] 配对=%s 对 %s' % (A_CODE, B_CODE))
    print('[初始化] 入场=%.2f 退出=%.2f 止损=%.2f 初始资金=%.2f' % (ENTRY_K, EXIT_K, STOP_K, INITIAL_CAPITAL))
    print('[初始化] 主图=%s 周期=%s' % (norm_code(getattr(ContextInfo, 'stockcode', '')), getattr(ContextInfo, 'period', '')))
    print('[初始化] 模式=仅虚拟 不下真实单 不做真实同步 单路径 含缺失调试的均衡日志')


# 每个 bar/tick 的主回调：
# - 处理日切/会话切换
# - 拉取价格
# - 收集锚定样本
# - 生成信号并执行虚拟再平衡
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
        st['actual_profile'] = 'EMPTY'  # 当前真实（虚拟）组合
        st['qty_a'] = 0  # A持仓数量
        st['qty_b'] = 0  # B持仓数量
        st['cash'] = st['last_equity']
        st['cur_ymd'] = None  # 当前交易日
        st['cur_session'] = None  # 当前会话（AM/PM）
        st['regime'] = 'NEUTRAL'  # 状态机（NEUTRAL/A_RICH/B_RICH）

    if st['day_ymd'] is not None:
        st['day_bars'] += 1

    if bar <= LOG_FIRST_BARS:
        print('[K线] 序号=%s 时间=%s 会话=%s' % (bar, str(cur_dt), str(sess)))

    # 拉取前心跳日志，即使后续持续 miss 也可观测。
    if LOG_PROGRESS_EVERY > 0 and (bar % LOG_PROGRESS_EVERY) == 0:
        print('[进度] 序号=%s 时间=%s 会话=%s 缺失=%s 零值=%s 会话外=%s 组合=%s' % (
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
            print('[缺失] 序号=%s 时间=%s 会话=%s 缺失=%s 拉取模式=%s' % (
                bar, str(cur_dt), str(sess), st['miss'], str(st.get('fetch_mode'))))
        return

    if a_px <= 0 or b_px <= 0:
        st['zero'] += 1
        if st['zero'] <= 3:
            print('[零值] 序号=%s 时间=%s a=%s b=%s' % (bar, str(cur_dt), str(a_px), str(b_px)))
        return

    st['last_a'] = a_px
    st['last_b'] = b_px
    st['last_equity'] = mark_equity(st, a_px, b_px)

    if not st['first_valid_logged']:
        st['first_valid_logged'] = True
        print('[首个有效] 序号=%s 时间=%s 会话=%s a=%.6f b=%.6f 权益=%.2f' % (
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
            print('[锚定累积] 日期=%s 会话=%s 时分秒=%s 样本数=%s 价差=%.10f' % (
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
            print('[锚定完成] 日期=%s 会话=%s 样本数=%s 均值=%.10f 波动=%.10f 入场阈=%.10f 退出阈=%.10f 止损阈=%.10f' % (
                ymd, sess, n, mu, sigma, st['entry_band'], st['exit_band'], st['stop_band']))
        else:
            print('[锚定失败] 日期=%s 会话=%s 样本数=%s 波动=%.12f 过小' % (ymd, sess, n, sigma))
            return

    if st['sigma'] is None or st['sigma'] <= MIN_SIGMA:
        return

    dev = spread - st['mu']
    regime = st['regime']

    st['signal_count'] += 1
    if st['signal_count'] <= 3 or (LOG_SIGNAL_EVERY > 0 and (st['signal_count'] % LOG_SIGNAL_EVERY) == 0):
        print('[信号] 日期=%s 会话=%s 时分秒=%s 价差=%.10f 偏离=%.10f 标准分=%.6f 组合=%s a=%.6f b=%.6f' % (
            ymd, sess, hms, spread, dev, dev / st['sigma'], st['actual_profile'], a_px, b_px))

    if regime == 'NEUTRAL':
        if dev >= st['entry_band']:
            st['regime'] = 'A_RICH'
            print('[入场] 日期=%s 会话=%s 时刻=%s 方向=A偏贵 -> 目标=B重仓 偏离=%.10f 标准分=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'B_HEAVY', a_px, b_px, 'enter_A_rich', ymd, sess, hms)
        elif dev <= -st['entry_band']:
            st['regime'] = 'B_RICH'
            print('[入场] 日期=%s 会话=%s 时刻=%s 方向=B偏贵 -> 目标=A重仓 偏离=%.10f 标准分=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'A_HEAVY', a_px, b_px, 'enter_B_rich', ymd, sess, hms)

    elif regime == 'A_RICH':
        if dev <= st['exit_band']:
            st['regime'] = 'NEUTRAL'  # 状态机（NEUTRAL/A_RICH/B_RICH）
            print('[退出] 日期=%s 会话=%s 时刻=%s 来自=A偏贵 偏离=%.10f 标准分=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'exit_A_rich', ymd, sess, hms)
        elif dev >= st['stop_band']:
            st['regime'] = 'NEUTRAL'  # 状态机（NEUTRAL/A_RICH/B_RICH）
            print('[止损] 日期=%s 会话=%s 时刻=%s 来自=A偏贵 偏离=%.10f 标准分=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'stop_A_rich', ymd, sess, hms)

    elif regime == 'B_RICH':
        if dev >= -st['exit_band']:
            st['regime'] = 'NEUTRAL'  # 状态机（NEUTRAL/A_RICH/B_RICH）
            print('[退出] 日期=%s 会话=%s 时刻=%s 来自=B偏贵 偏离=%.10f 标准分=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'exit_B_rich', ymd, sess, hms)
        elif dev <= -st['stop_band']:
            st['regime'] = 'NEUTRAL'  # 状态机（NEUTRAL/A_RICH/B_RICH）
            print('[止损] 日期=%s 会话=%s 时刻=%s 来自=B偏贵 偏离=%.10f 标准分=%.6f' % (
                ymd, sess, hms, dev, dev / st['sigma']))
            apply_virtual_profile(st, 'NEUTRAL', a_px, b_px, 'stop_B_rich', ymd, sess, hms)

    st['last_equity'] = mark_equity(st, a_px, b_px)


# 生命周期结束：打印最终汇总。
def stop(ContextInfo):
    st = ContextInfo.user_data
    print_day_summary(st, st.get('last_a'), st.get('last_b'), 'stop')
    print('[回测汇总] 原因=停止 K线数=%s 缺失=%s 零值=%s 会话外=%s 最终权益=%.2f 组合=%s A持仓=%s B持仓=%s 现金=%.2f' % (
        st.get('bars', 0), st.get('miss', 0), st.get('zero', 0), st.get('out', 0),
        st.get('last_equity', INITIAL_CAPITAL), st.get('actual_profile', 'NA'),
        st.get('qty_a', 0), st.get('qty_b', 0), st.get('cash', INITIAL_CAPITAL)))
