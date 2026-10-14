"""MarketEye 모의투자 — 지정 시각 체결가 계산·신호 정확도 평가.

'진짜 주식처럼' 체결한다:
  · 장중 주문      → 그 시각 가격(최근이면 실시간가, 과거면 분봉, 분봉이 없으면 일봉 보간)
  · 장 시작 전 주문 → 그날 시가
  · 장 마감 후·주말·휴장 주문 → 다음 거래일 시가
  · 아직 오지 않은 시각/거래일 → 대기(pending). 프론트가 나중에 다시 물어 체결한다.

정확도 점검은 주문마다 '그 시점 직전 종가까지의 일봉'으로 차트 점수(_technical_signal,
백테스트와 같은 엔진)를 다시 계산해 기록하고, 이후 수익·지수 대비 초과수익·매매플랜
(목표/손절) 도달 여부를 일봉으로 평가한다. 미래 데이터를 보지 않으므로 과거 날짜 주문도
점수 자체에는 사후 편향이 없다(종목 선택 편향은 프론트에서 '과거 지정 주문'으로 분리 표시).
"""
import bisect
import concurrent.futures
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo

import yfinance as yf

from infra import log, timed, yf_call, set_tag, _tag, _cache_get, _cache_set
from providers import _fetch_naver, _fetch_naver_daily
from signals import safe_val, _technical_signal

MARKET_TZ = {'kr': ZoneInfo('Asia/Seoul'), 'us': ZoneInfo('America/New_York')}
SESSION = {'kr': (dtime(9, 0), dtime(15, 30)), 'us': (dtime(9, 30), dtime(16, 0))}
DAILY_TTL = 600
SIGNAL_MIN_BARS = 70        # 백테스트 START와 동일 — 이보다 짧으면 점수 미산출
LIVE_WINDOW = timedelta(minutes=3)   # 이 안의 장중 주문은 실시간가로 체결


def market_of(ticker):
    return 'kr' if ticker.upper().endswith(('.KS', '.KQ')) else 'us'


def _benchmark(ticker):
    t = ticker.upper()
    if t.endswith('.KS'):
        return '^KS11', 'KOSPI'
    if t.endswith('.KQ'):
        return '^KQ11', 'KOSDAQ'
    return '^GSPC', None


def _period_for(start):
    """start 날짜 이전 1년+ 일봉(점수 계산용)까지 덮는 야후 기간과 네이버 봉 수."""
    days = (date.today() - start).days + 400
    for period, span in (('2y', 730), ('5y', 1826), ('10y', 3652)):
        if days <= span:
            return period, int(span * 0.7)
    return 'max', 6000


def _frame_to_bars(df):
    """야후/네이버 일봉 DataFrame → 날짜 오름차순 dict 배열(거래소 현지 날짜)."""
    cols = {c: [float(v) for v in df[c].tolist()] for c in ('Open', 'High', 'Low', 'Close', 'Volume')}
    dates = [ts.date() for ts in df.index]
    return {'dates': dates, 'open': cols['Open'], 'high': cols['High'], 'low': cols['Low'],
            'close': cols['Close'], 'vol': cols['Volume']}


def daily_bars(symbol, start, naver_code=None):
    """start 이전 1년 이상을 포함한 일봉(시가 포함). 야후 → (한국) 네이버 순. 실패 시 예외.
    야후는 auto_adjust=False(분할만 반영, 배당 미반영) — 체결가가 당시 실제 호가와 같게."""
    period, n_naver = _period_for(start)
    key = ('pdaily', symbol, period)
    cached = _cache_get(key, DAILY_TTL)
    if cached is not None:
        return cached
    bars, err = None, None
    try:
        with timed(f'yf.history({period},paper) {symbol}'):
            df = yf_call(lambda: yf.Ticker(symbol).history(period=period, interval='1d', auto_adjust=False),
                         f'yf.history({period},paper) {symbol}')
        if len(df) >= 2:
            bars = _frame_to_bars(df)
    except Exception as e:
        err = e
    if bars is None and naver_code:
        try:
            bars = _frame_to_bars(_fetch_naver_daily(naver_code, n_naver))
            log(f'{symbol} 모의투자 일봉 → 네이버 폴백 ({len(bars["dates"])}봉): {err}', 'WARN')
        except Exception as ne:
            err = ne
    if bars is None:
        raise err or ValueError(f'{symbol} 일봉 없음')
    _cache_set(key, bars)
    return bars


def _live_quote(ticker):
    """실시간 {'price','open','date'} (거래소 현지 날짜). 실패 시 None."""
    try:
        if market_of(ticker) == 'kr':
            nv = _fetch_naver(ticker.split('.')[0])
            if nv.get('price'):
                td = nv.get('tradeDate')
                return {'price': nv['price'], 'open': nv.get('open'),
                        'date': date.fromisoformat(td) if td else None}
            return None
        fi = yf_call(lambda: yf.Ticker(ticker).fast_info, f'yf.fast_info(paper) {ticker}')
        p = safe_val(fi.last_price)
        if p and p > 0:
            return {'price': p, 'open': safe_val(fi.open), 'date': None}
    except Exception as e:
        log(f'{ticker} 모의투자 실시간가 실패: {e}', 'WARN')
    return None


def _intraday_price(ticker, at):
    """at(거래소 현지 tz-aware) 시각의 분봉 가격. 야후 분봉 보관기간을 넘거나 실패하면 None."""
    age = datetime.now(at.tzinfo) - at
    if age <= timedelta(days=6):
        interval, step = '1m', timedelta(minutes=1)
    elif age <= timedelta(days=58):
        interval, step = '5m', timedelta(minutes=5)
    elif age <= timedelta(days=720):
        interval, step = '60m', timedelta(hours=1)
    else:
        return None
    d = at.date()
    try:
        df = yf_call(lambda: yf.Ticker(ticker).history(start=d.isoformat(),
                                                       end=(d + timedelta(days=1)).isoformat(),
                                                       interval=interval, auto_adjust=False),
                     f'yf.history({interval},paper) {ticker}')
    except Exception:
        return None
    if df is None or df.empty:
        return None
    # at을 포함하는 봉: 봉 안에서 시가→종가를 경과 비율로 보간(1분봉이면 사실상 그 분 가격)
    for ts, row in zip(df.index, df.itertuples()):
        start = ts.to_pydatetime().astimezone(at.tzinfo)
        if start <= at < start + step:
            frac = (at - start) / step
            return float(row.Open) + (float(row.Close) - float(row.Open)) * frac, interval
    return None


def _signal_at(bars, k):
    """k번째 봉(체결일) 직전 종가까지만으로 계산한 차트 점수·매매플랜(미래 데이터 미사용)."""
    if k < SIGNAL_MIN_BARS:
        return None
    try:
        ts = _technical_signal(bars['close'][:k], bars['high'][:k], bars['low'][:k], bars['vol'][:k],
                               rs_60=None, weekly_up=None, with_reasons=False)
    except Exception as e:
        log(f'모의투자 과거 점수 계산 실패: {e}', 'WARN')
        return None
    plan = ts.get('plan') or {}
    return {'tech': ts['tech_score'], 'regime': ts.get('regime'),
            'stop': plan.get('stop'), 'target': plan.get('target'),
            'asof': bars['dates'][k - 1].isoformat()}


def _filled(price, at, src, note, bars=None, k=None):
    out = {'status': 'filled', 'price': round(float(price), 4), 'fill_at': at.isoformat(),
           'fill_date': at.date().isoformat(), 'src': src, 'note': note, 'signal': None}
    if bars is not None and k is not None:
        out['signal'] = _signal_at(bars, k)
    return out


def _pending(note):
    return {'status': 'pending', 'note': note}


def price_at(ticker, at_utc):
    """at_utc(tz-aware) 시각에 시장가 주문을 냈을 때의 체결 결과.
    {'status': 'filled', price, fill_at, fill_date, src, note, signal} | {'status': 'pending', note}.
    해당 시세를 끝내 알 수 없으면 예외(ValueError)."""
    ticker = ticker.upper()
    mkt = market_of(ticker)
    tz = MARKET_TZ[mkt]
    at = at_utc.astimezone(tz)
    now = datetime.now(tz)
    if at > now + timedelta(minutes=1):
        return _pending('예약 주문 — 지정 시각이 되면 체결')
    open_t, close_t = SESSION[mkt]
    d = at.date()
    weekday = d.weekday() < 5
    in_session = weekday and open_t <= at.time() < close_t

    # ① 방금 낸 장중 주문 → 실시간가
    if in_session and now - at <= LIVE_WINDOW:
        q = _live_quote(ticker)
        # 한국은 네이버 거래일로 휴장일(직전 종가가 실시간처럼 보임)을 걸러낸다
        if q and q.get('date') in (None, d):
            bars, k = _bars_and_index(ticker, d)
            return _filled(q['price'], at, 'live', '실시간 시장가 체결', bars, k)

    bars = daily_bars(ticker, d, ticker.split('.')[0] if mkt == 'kr' else None)
    dates = bars['dates']
    if d < dates[0]:
        raise ValueError(f'{d} 시세 없음(상장 전이거나 데이터 범위 밖)')
    i = bisect.bisect_left(dates, d)
    is_trading_day = i < len(dates) and dates[i] == d

    if is_trading_day and at.time() < open_t:
        return _filled(bars['open'][i], at.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0),
                       'open', '장 시작 전 주문 → 당일 시가 체결', bars, i)

    if is_trading_day and in_session:
        ip = _intraday_price(ticker, at)
        if ip:
            return _filled(ip[0], at, f'intraday_{ip[1]}', f'{ip[1]} 분봉 기준 체결', bars, i)
        # 분봉이 없으면 일봉 시가→종가를 장중 경과 비율로 보간(오늘이면 지금까지 경과 기준)
        o_dt = at.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
        c_dt = at.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
        end = min(c_dt, now) if d == now.date() else c_dt
        frac = max(0.0, min(1.0, (at - o_dt) / (end - o_dt))) if end > o_dt else 1.0
        px = bars['open'][i] + (bars['close'][i] - bars['open'][i]) * frac
        return _filled(px, at, 'daily_interp', '분봉 없음 → 일봉 시가·종가 보간(근사)', bars, i)

    # ② 장 마감 후·주말·휴장 → 다음 거래일 시가
    j = i + 1 if is_trading_day else i
    if j < len(dates):
        nd = dates[j]
        fill = datetime(nd.year, nd.month, nd.day, open_t.hour, open_t.minute, tzinfo=tz)
        return _filled(bars['open'][j], fill, 'next_open', '장외 주문 → 다음 거래일 시가 체결', bars, j)

    # ③ 다음 거래일 봉이 아직 없음: 오늘 장이 이미 열렸으면 실시간 시가, 아니면 대기
    today = now.date()
    if today > d and today.weekday() < 5 and now.time() >= open_t:
        q = _live_quote(ticker)
        # 거래일이 확인되는 경우(한국·네이버)만 — 휴장일에 직전 시가로 체결되는 걸 막는다
        if q and q.get('open') and q.get('date') == today:
            fill = now.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
            return _filled(q['open'], fill, 'next_open', '장외 주문 → 오늘 시가 체결', bars, len(dates))
    if is_trading_day and d == today:
        # 오늘 장중 주문인데 분봉·일봉이 모두 막혔다 → 실시간가 근사
        q = _live_quote(ticker)
        if q:
            return _filled(q['price'], at, 'live_approx', '시세 지연 → 현재가로 근사 체결', bars, i)
    return _pending('다음 거래일 개장 대기 — 시가로 체결 예정')


def _bars_and_index(ticker, d):
    """실시간 체결의 점수 계산용 (일봉·체결일 인덱스). 실패해도 체결은 진행(점수만 None)."""
    try:
        bars = daily_bars(ticker, d, ticker.split('.')[0] if market_of(ticker) == 'kr' else None)
        return bars, bisect.bisect_left(bars['dates'], d)
    except Exception as e:
        log(f'{ticker} 실시간 체결 점수 계산용 일봉 실패: {e}', 'WARN')
        return None, None


# ── 평가 ─────────────────────────────────────────────────────────────
HORIZON = 20     # 고정 평가 기간(거래일) — 백테스트 HOLD_MAX와 같아 결과를 비교할 수 있다


def _ret(a, b):
    return (b / a - 1) * 100 if a else None


def _evaluate_ticker(ticker, lots):
    ticker = ticker.upper()
    mkt = market_of(ticker)
    start = min(date.fromisoformat(l['fill_date']) for l in lots)
    bars = daily_bars(ticker, start, ticker.split('.')[0] if mkt == 'kr' else None)
    dates, close = bars['dates'], bars['close']
    q = _live_quote(ticker)
    last = q['price'] if q else close[-1]

    bsym, bnaver = _benchmark(ticker)
    try:
        bb = daily_bars(bsym, start, bnaver)
    except Exception as e:
        log(f'{bsym} 지수 일봉 실패(초과수익 생략): {e}', 'WARN')
        bb = None

    def bench_close(d):
        if not bb:
            return None
        k = bisect.bisect_right(bb['dates'], d) - 1
        return bb['close'][k] if k >= 0 else None

    out = {}
    for l in lots:
        fd = date.fromisoformat(l['fill_date'])
        price = float(l['price'])
        k = bisect.bisect_left(dates, fd)           # 체결일 봉(없으면 다음 봉)
        after = range(k + 1, len(dates))            # 체결 다음 날부터(보수적)
        stop, target = l.get('stop'), l.get('target')
        plan, plan_days = 'open', None
        for j in after:
            if stop is not None and bars['low'][j] <= stop:
                plan, plan_days = 'stop', j - k; break
            if target is not None and bars['high'][j] >= target:
                plan, plan_days = 'target', j - k; break
        if stop is None and target is None:
            plan = None
        held = max(0, len(dates) - 1 - k)
        b0, b_last = bench_close(fd), (bb['close'][-1] if bb else None)
        r = {
            'last': last,
            'ret': _ret(price, last),
            'bench_ret': _ret(b0, b_last) if (b0 and b_last) else None,
            'held_days': held,
            'plan': plan, 'plan_days': plan_days,
            'mfe': _ret(price, max(bars['high'][j] for j in after)) if held else None,
            'mae': _ret(price, min(bars['low'][j] for j in after)) if held else None,
        }
        if k + HORIZON < len(dates):
            r['fwd'] = _ret(price, close[k + HORIZON])
            b1 = bench_close(dates[k + HORIZON])
            r['bench_fwd'] = _ret(b0, b1) if (b0 and b1) else None
        out[l['id']] = r
    return out


def evaluate(lots):
    """lots: [{id, ticker, fill_date, price, stop?, target?}] → {id: 평가}. 종목별 병렬, 실패 종목은 error."""
    by_t = {}
    for l in lots:
        by_t.setdefault(l['ticker'].upper(), []).append(l)
    parent = _tag()
    result = {}

    def run(t):
        set_tag(parent)
        return t, _evaluate_ticker(t, by_t[t])

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(run, t) for t in by_t]
        for f, t in zip(futs, by_t):
            try:
                result.update(f.result()[1])
            except Exception as e:
                log(f'모의투자 평가 {t} 실패: {e}', 'WARN')
                for l in by_t[t]:
                    result[l['id']] = {'error': str(e)}
    return result
