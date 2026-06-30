from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yfinance as yf
import requests
import traceback
import os
import time
import threading

# 공용 인프라(로깅·타이밍·TTL 캐시·JSON 방어막)는 infra.py로 분리.
from infra import (
    log, timed, set_tag, _tag, _req_seq,
    _cache_get, _cache_set, _cache_get_stale, yf_call, is_rate_limited,
    SafeJSONProvider,
)
# 외부 데이터 수집층(KRX·네이버·야후 조회)은 providers.py로 분리.
from providers import (
    _search_krx, _is_korean, _yahoo_search,
    _fetch_stock, _calc_per_pbr, _fetch_naver,
)
# 순수 신호 엔진은 signals.py로 분리 (동작 동일). app.py는 라우트·조립 담당.
from signals import (
    safe_val,
    _technical_signal,
    _fundamental_signal,
    _composite_signal,
)

app = Flask(__name__)
CORS(app)

app.json = SafeJSONProvider(app)


# ── 요청 타이밍 로그 (요청별 태그 부여) ──────────────────────
@app.before_request
def _req_start():
    n = next(_req_seq)
    # 태그 = '#순번 마지막경로조각' → 동시 요청 구분용 (예: '#7 010120.KS')
    set_tag(f'#{n} {request.path.rsplit("/", 1)[-1][:18]}')
    request._t0 = time.time()
    request._rtag = _tag()
    log(f'요청 시작 {request.method} {request.path}', 'DEBUG')

@app.after_request
def _req_end(response):
    ms = int((time.time() - getattr(request, '_t0', time.time())) * 1000)
    mark = ' 🐢 매우느림' if ms > 10000 else (' ⏱ 느림' if ms > 3000 else '')
    lvl = 'WARN' if ms > 3000 else 'INFO'
    log(f'요청 완료 {request.method} {request.path} → {response.status_code} ({ms}ms){mark}', lvl)
    return response

# 외부 데이터 수집(KRX 종목·검색·한글명, 네이버 실시간 시세, 야후 종목 데이터)은
# providers.py로 분리됨. KRX 목록 로드·갱신 스레드도 providers import 시 시작된다.

@app.route('/api/search')
def search_ticker():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    try:
        seen = set()
        results = []

        def add(ticker, name, exchange, typ='EQUITY'):
            if ticker and ticker not in seen:
                seen.add(ticker)
                results.append({'ticker': ticker, 'name': name, 'exchange': exchange, 'type': typ})

        # 1차: KRX 로컬 검색 (한국어/종목코드 모두 커버)
        for s in _search_krx(query, limit=8):
            add(s['ticker'], s['name'], s['market'])
        log(f'검색 "{query}" → KRX 로컬 {len(results)}건', 'DEBUG')

        # 2차: 한국어가 아니면 야후 글로벌 검색으로 해외 종목 추가
        if not _is_korean(query) and len(results) < 10:
            for q in _yahoo_search(query):
                qtype = q.get('quoteType', '')
                if qtype not in ('EQUITY', 'ETF', 'INDEX'):
                    continue
                add(q.get('symbol',''), q.get('longname') or q.get('shortname') or '', q.get('exchange',''), qtype)

        return jsonify(results[:10])
    except Exception as e:
        log(f'검색 "{query}" 실패: {e}', 'ERROR')
        traceback.print_exc()
        return jsonify([])


# 전체 데이터 TTL (초). 환경변수로 조정 가능.
STOCK_TTL = int(os.environ.get('STOCK_TTL', '60'))
PRICE_TTL = int(os.environ.get('PRICE_TTL', '15'))
# 백테스트 검증 기간 — 길수록 더 많은 시장 국면(강세·약세·횡보)을 포함해 신뢰도↑.
# 기본 5년(2022 약세장 포함). 너무 느리면 '3y' 등으로 낮출 수 있다.
BACKTEST_PERIOD = os.environ.get('BACKTEST_PERIOD', '5y')


@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    key = ticker.upper()
    cached = _cache_get(('stock', key), STOCK_TTL)
    if cached is not None:
        log(f'stock {key} 캐시 히트', 'DEBUG')
        return jsonify({'success': True, 'data': cached})
    try:
        with timed(f'stock 전체조회 {key}'):
            data = _fetch_stock(ticker)
        _cache_set(('stock', key), data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        # 레이트리밋 등으로 신규 조회 실패 시, 만료된 캐시라도 있으면 그 값을 돌려준다.
        # (빈 화면·달러 표기 대신 직전 정상값을 보여주는 graceful degradation)
        stale = _cache_get_stale(('stock', key))
        if stale is not None:
            log(f'stock {key} 조회 실패 → stale 캐시 반환: {e}', 'WARN')
            return jsonify({'success': True, 'data': stale, 'stale': True})
        log(f'stock {key} 실패: {e}', 'ERROR')
        traceback.print_exc()
        status = 429 if is_rate_limited(e) else 500
        return jsonify({'success': False, 'error': str(e)}), status


@app.route('/api/price/<ticker>')
def get_price(ticker):
    """가격·등락률만 빠르게 반환 (fast_info). 자동 새로고침용 경량 엔드포인트."""
    key = ticker.upper()
    cached = _cache_get(('price', key), PRICE_TTL)
    if cached is not None:
        log(f'price {key} 캐시 히트', 'DEBUG')
        return jsonify({'success': True, 'data': cached})
    try:
        t = yf.Ticker(key)
        with timed(f'yf.fast_info(price) {key}', warn_ms=1500, slow_ms=3000):
            fi = yf_call(lambda: t.fast_info, f'yf.fast_info(price) {key}')
        last = safe_val(fi.last_price)
        prev = safe_val(fi.previous_close)
        change = (last - prev) if (last is not None and prev) else None
        change_pct = (change / prev * 100) if (change is not None and prev) else None
        data = {
            'price': last,
            'change': change,
            'changePercent': change_pct,
            'open': safe_val(fi.open),
            'prevClose': prev,
            'dayLow': safe_val(fi.day_low),
            'dayHigh': safe_val(fi.day_high),
            'volume': safe_val(fi.last_volume),
        }
        _cache_set(('price', key), data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        stale = _cache_get_stale(('price', key))
        if stale is not None:
            log(f'price {key} 조회 실패 → stale 캐시 반환: {e}', 'WARN')
            return jsonify({'success': True, 'data': stale, 'stale': True})
        log(f'price {key} 실패: {e}', 'ERROR')
        traceback.print_exc()
        status = 429 if is_rate_limited(e) else 500
        return jsonify({'success': False, 'error': str(e)}), status


@app.route('/api/batch')
def get_batch():
    """여러 종목을 병렬로 조회."""
    tickers = request.args.get('tickers', '')
    ticker_list = [t.strip() for t in tickers.split(',') if t.strip()]
    results = {}
    lock = threading.Lock()
    parent_tag = _tag()   # 워커 스레드에 부모 요청 태그 전파

    def work(ticker):
        key = ticker.upper()
        set_tag(f'{parent_tag}»{key}')   # 워커 로그도 부모 요청으로 추적 가능
        cached = _cache_get(('stock', key), STOCK_TTL)
        try:
            if cached is not None:
                data = cached
            else:
                with timed(f'batch 조회 {key}'):
                    data = _fetch_stock(ticker)
                _cache_set(('stock', key), data)
            payload = {'success': True, 'data': data}
        except Exception as e:
            log(f'batch {key} 실패: {e}', 'ERROR')
            payload = {'success': False, 'error': str(e)}
        with lock:
            results[key] = payload

    log(f'batch {len(ticker_list)}종목 병렬조회 시작: {ticker_list}', 'DEBUG')
    threads = [threading.Thread(target=work, args=(t,)) for t in ticker_list]
    for th in threads: th.start()
    for th in threads: th.join()
    return jsonify(results)


# ── 진입 시점 신호 분석 ─────────────────────────────────────
# 순수 점수 엔진(_technical_signal·_fundamental_signal·_composite_signal 및 지표함수
# _ema/_sma/_rsi_series/_atr/_pivots)은 signals.py로 분리됨. 여기에는 네트워크·캐시에
# 의존하는 벤치마크 조회와 조립(_signal_base·라우트)만 둔다.

def _benchmark_symbol(ticker):
    tl = (ticker or '').upper()
    if tl.endswith('.KS'): return '^KS11'   # KOSPI
    if tl.endswith('.KQ'): return '^KQ11'   # KOSDAQ
    return '^GSPC'                            # S&P 500 (해외 기본)

def _benchmark_closes(symbol):
    """시장지수 종가(1년)를 캐싱해 상대강도 계산에 재사용 (1시간 TTL)."""
    cached = _cache_get(('bench', symbol), 3600)
    if cached is not None:
        return cached
    try:
        with timed(f'yf.history(지수 {symbol})', warn_ms=2000, slow_ms=4000):
            h = yf_call(lambda: yf.Ticker(symbol).history(period='1y', interval='1d'),
                        f'yf.history(지수 {symbol})')
        closes = [float(v) for v in h['Close'].tolist()] if not h.empty else []
    except Exception as e:
        log(f'지수 {symbol} 조회 실패: {e}', 'WARN')
        closes = []
    _cache_set(('bench', symbol), closes)
    return closes


def _signal_base(ticker):
    """신호 계산 중 '장중 불변·고비용' 부분만 캐싱한다.
    (야후 2년 일봉 배열·주봉추세·info·야후 PER/PBR — 모두 장중에 바뀌지 않거나
    분기 단위로만 바뀜) 실시간 가격·네이버 오버레이·점수 계산은 캐싱하지 않고
    매 요청마다 새로 한다. 데이터 부족 시 None."""
    cached = _cache_get(('sigbase', ticker), 1800)
    if cached is not None:
        log(f'signal_base {ticker} 캐시 히트', 'DEBUG')
        return cached

    t = yf.Ticker(ticker)
    # 200일선·기울기 판정을 위해 2년치 일봉 확보 (signal의 핵심 비용)
    try:
        with timed(f'yf.history(2y) {ticker}'):
            hist = yf_call(lambda: t.history(period='2y', interval='1d'),
                           f'yf.history(2y) {ticker}')
    except Exception as e:
        # 레이트리밋 등으로 일봉을 못 받으면 만료된 base라도 재사용 (신호 유지)
        stale = _cache_get_stale(('sigbase', ticker))
        if stale is not None:
            log(f'signal_base {ticker} 조회 실패 → stale base 재사용: {e}', 'WARN')
            return stale
        raise
    if hist.empty or len(hist) < 60:
        log(f'signal_base {ticker} 데이터 부족 (rows={len(hist)})', 'WARN')
        return None
    closes = [float(v) for v in hist['Close'].tolist()]
    highs  = [float(v) for v in hist['High'].tolist()]
    lows   = [float(v) for v in hist['Low'].tolist()]
    vols   = [float(v) for v in hist['Volume'].tolist()]

    # 주봉(상위 시간프레임) 추세 — 장중 거의 불변
    weekly_up = None
    try:
        wclos = [float(v) for v in hist['Close'].resample('W').last().dropna().tolist()]
        if len(wclos) >= 34:
            wma30, wma30_prev = sum(wclos[-30:]) / 30, sum(wclos[-34:-4]) / 30
            weekly_up = wclos[-1] > wma30 and wma30 > wma30_prev
    except Exception:
        pass

    # 펀더멘털 info + 야후 PER/PBR (분기 재무 기반, 장중 불변)
    with timed(f'yf.info(signal) {ticker}'):
        info = yf_call(lambda: t.info, f'yf.info(signal) {ticker}')
    try:
        fi = yf_call(lambda: t.fast_info, f'yf.fast_info(signal) {ticker}')
        if fi.last_price:
            info['currentPrice'] = fi.last_price
    except Exception:
        pass
    with timed(f'PER/PBR(signal) {ticker}', warn_ms=2000, slow_ms=4000):
        per, pbr, _, _ = _calc_per_pbr(info, t)

    base = {
        'closes': closes, 'highs': highs, 'lows': lows, 'vols': vols,
        'last_date': hist.index[-1].date().isoformat(),
        'weekly_up': weekly_up,
        'info': info, 'per': per, 'pbr': pbr,
    }
    _cache_set(('sigbase', ticker), base)
    return base


@app.route('/api/signal/<path:ticker>')
def signal(ticker):
    # 신호 계산 결과 5분 캐시 (Render 배포 성능 개선)
    cached = _cache_get(('signal', ticker), 300)
    if cached:
        log(f'signal {ticker} 캐시 히트', 'DEBUG')
        return jsonify({'success': True, 'data': cached})

    try:
        with timed(f'signal_base {ticker}'):
            base = _signal_base(ticker)
        if base is None:
            return jsonify({'success': False, 'error': '데이터 부족 (최소 60거래일 필요)'}), 422

        # 캐시된 배열·info는 매 요청마다 복사 후 실시간 값으로 오버레이한다.
        # (결과는 캐싱하지 않으므로 종목에 들어갈 때마다 네이버 실시간이 반영됨)
        closes = list(base['closes']); highs = list(base['highs'])
        lows = list(base['lows']); vols = list(base['vols'])
        weekly_up = base['weekly_up']
        info = dict(base['info'])
        per, pbr = base['per'], base['pbr']
        n = len(closes)

        # 한국 종목: 마지막 봉을 네이버 실시간으로 교체/추가하고 PER/PBR·시총을 최신화.
        # (야후 KRX 15~20분 지연 → 차트·기술점수·진입가·밸류에이션이 실시간 반영)
        if ticker.upper().endswith(('.KS', '.KQ')):
            try:
                with timed(f'네이버 실시간 {ticker}', warn_ms=2000, slow_ms=4000):
                    nv = _fetch_naver(ticker.split('.')[0])
                rt = nv.get('price')
                if rt:
                    dh, dl, vol = nv.get('dayHigh'), nv.get('dayLow'), nv.get('volume')
                    if nv.get('tradeDate') == base['last_date']:
                        # 야후에 이미 오늘 봉이 있으면(지연된 값) 실시간으로 갱신
                        closes[-1] = rt
                        highs[-1] = max(highs[-1], dh or rt, rt)
                        lows[-1] = min(lows[-1], dl or rt, rt)
                        if vol:
                            vols[-1] = vol
                    else:
                        # 야후에 오늘 봉이 아직 없으면 실시간 봉을 추가
                        closes.append(rt); highs.append(dh or rt)
                        lows.append(dl or rt); vols.append(vol or 0.0)
                    n = len(closes)
                if nv.get('per') is not None:
                    per = nv.get('per')
                if nv.get('pbr') is not None:
                    pbr = nv.get('pbr')
                # 네이버 실시간 시총으로 시총 파생 밸류에이션도 최신화 (PSR·EV/EBITDA)
                nv_mcap, nv_fpe = nv.get('marketCap'), nv.get('forwardPer')
                if nv_mcap is not None:
                    info['marketCap'] = nv_mcap
                    rev = safe_val(info.get('totalRevenue'))
                    if rev and rev > 0:
                        info['priceToSalesTrailing12Months'] = nv_mcap / rev
                    ebitda = safe_val(info.get('ebitda'))
                    if ebitda and ebitda > 0:
                        ev = nv_mcap + (safe_val(info.get('totalDebt')) or 0) - (safe_val(info.get('totalCash')) or 0)
                        info['enterpriseToEbitda'] = ev / ebitda
                if nv_fpe is not None:
                    info['forwardPE'] = nv_fpe
            except Exception as e:
                log(f'네이버 실시간 {ticker} 실패(야후값 폴백): {e}', 'WARN')

        # 시장 대비 상대강도 (지수 종가는 1시간 캐시)
        rs_60 = None
        try:
            bclos = _benchmark_closes(_benchmark_symbol(ticker))
            if len(bclos) > 61 and n > 61:
                rs_60 = (closes[-1] / closes[-61] - 1) - (bclos[-1] / bclos[-61] - 1)
        except Exception:
            pass

        # 12-1 절대 모멘텀 (최근 12개월 수익에서 마지막 1개월 제외 — 단기 반전 회피).
        # 가격만으로 계산하며 RS 슬롯에 통합 입력된다(새 가중치 없음). 데이터 부족 시 None.
        mom_12_1 = None
        if n > 252 and closes[-252] > 0:
            mom_12_1 = closes[-21] / closes[-252] - 1

        # 유동성(신뢰도 전용): 최근 20일 일평균 거래대금. 빈약하면 신뢰도만 낮춘다(점수 불변).
        # 통화 스케일이 다르므로 KR(원)/해외(달러) 임계를 분리. 거래대금 미상이면 1.0(영향 없음).
        liq_factor = 1.0
        try:
            if len(vols) >= 20 and closes[-1] > 0:
                turnover = (sum(vols[-20:]) / 20) * closes[-1]
                if ticker.upper().endswith(('.KS', '.KQ')):
                    liq_factor = (1.0 if turnover >= 1e9 else 0.85 if turnover >= 3e8
                                  else 0.7 if turnover >= 1e8 else 0.5)
                else:
                    liq_factor = (1.0 if turnover >= 2e7 else 0.85 if turnover >= 5e6
                                  else 0.7 if turnover >= 1e6 else 0.5)
        except Exception:
            liq_factor = 1.0

        # 점수 계산은 순수 연산 — 통째로 한 번만 측정(보통 수 ms, 느리면 데이터 이상)
        with timed(f'signal 점수계산 {ticker}', warn_ms=500, slow_ms=1500):
            # 기술적 매수 점수 (백테스트와 동일한 순수 엔진을 공유)
            ts = _technical_signal(closes, highs, lows, vols, rs_60, weekly_up, mom_12_1=mom_12_1)
            tech_score = ts['tech_score']
            # 5대 축 전문가형 펀더멘털 점수
            fs = _fundamental_signal(info, per, pbr)
            fund_score = fs['fund_score']

        # 가치 점수 신뢰도: 5대 축 중 데이터가 있는 축의 가중 비율 (0~1)
        _pw = {'valuation': 0.28, 'profitability': 0.24, 'growth': 0.20, 'health': 0.16, 'cashflow': 0.12}
        fund_conf = sum(_pw[k] for k, v in fs['pillars'].items() if v is not None)
        # 전문가식 종합 점수 (차트·가치를 관계·신뢰도·유동성까지 고려해 합성)
        comp = _composite_signal(tech_score, fund_score, ts['regime'], ts['plan'].get('rr'),
                                 fund_conf, liq_factor)
        combined = comp['score']

        def lab(s):
            if s >= 68: return '매수 고려'
            if s >= 48: return '관망'
            return '주의'
        label = comp['verdict_label']

        data = {
            'combined': combined,
            'composite': comp,
            'tech_score': tech_score,
            'fund_score': fund_score,
            'signal': label,
            'tech_label': lab(tech_score),
            'fund_label': lab(fund_score),
            'regime': ts['regime'],
            'regime_label': ts['regime_label'],
            'reasons': ts['reasons'],
            'indicators': ts['indicators'],
            'plan': ts['plan'],
            'fundamentals': fs,
        }
        _cache_set(('signal', ticker), data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        log(f'signal {ticker} 실패: {e}', 'ERROR')
        traceback.print_exc()
        status = 429 if is_rate_limited(e) else 500
        return jsonify({'success': False, 'error': str(e)}), status


@app.route('/api/backtest/<path:ticker>')
def backtest(ticker):
    """실시간 신호와 '동일한' 기술 엔진(_technical_signal)을 과거 전 구간에 적용해
    매수 규칙의 성과를 검증한다. 점수 >= 68(매수 고려)에서 진입, ATR 매매플랜의
    손절/목표 또는 최대 보유기간 도달 시 청산. 상대강도·주봉은 백테스트에서 제외(중립)."""
    cached = _cache_get(('backtest', ticker), 3600)
    if cached:
        log(f'backtest {ticker} 캐시 히트', 'DEBUG')
        return jsonify({'success': True, 'data': cached})
    try:
        t = yf.Ticker(ticker)
        with timed(f'yf.history({BACKTEST_PERIOD},backtest) {ticker}'):
            hist = yf_call(lambda: t.history(period=BACKTEST_PERIOD, interval='1d'),
                           f'yf.history({BACKTEST_PERIOD},backtest) {ticker}')
        if hist.empty or len(hist) < 120:
            return jsonify({'success': False, 'error': '데이터 부족 (최소 120거래일 필요)'}), 422

        closes = [float(v) for v in hist['Close'].tolist()]
        highs  = [float(v) for v in hist['High'].tolist()]
        lows   = [float(v) for v in hist['Low'].tolist()]
        vols   = [float(v) for v in hist['Volume'].tolist()]
        n = len(closes)

        BUY_TH, HOLD_MAX, START = 68, 20, 70   # 매수기준 / 최대보유(거래일) / 시작 인덱스
        COST_PCT = 0.35   # 왕복 매매비용(수수료+거래세+슬리피지) 가정 — 보수적. 순수익에 반영.

        # 신호 엔진을 매 거래일 1회만 계산해 매매시뮬·구간검증이 공유한다.
        # (예전엔 두 단계가 각각 O(n²)로 따로 돌려 장기간이 부담 → 한 번만 계산해 재사용)
        _bt0 = time.time()
        day_sig = [None] * n
        for k in range(START, n):
            day_sig[k] = _technical_signal(closes[:k + 1], highs[:k + 1], lows[:k + 1],
                                           vols[:k + 1], rs_60=None, weekly_up=None, with_reasons=False)
        log(f'backtest 점수계산 {ticker} {int((time.time()-_bt0)*1000)}ms (n={n})',
            'WARN' if (time.time() - _bt0) > 3.0 else 'INFO')

        # ── 매매 시뮬레이션 (점수>=기준 진입, 손절/목표/보유만료 청산, 왕복비용 차감) ──
        trades = []
        i = START
        while i < n - 1:
            sub = day_sig[i]
            if sub['tech_score'] >= BUY_TH:
                entry = closes[i]
                stop, target = sub['plan']['stop'], sub['plan']['target']
                exit_price, exit_reason, bars = None, None, 0
                for j in range(i + 1, min(i + 1 + HOLD_MAX, n)):
                    bars = j - i
                    if stop is not None and lows[j] <= stop:        # 손절 우선(보수적)
                        exit_price, exit_reason = stop, '손절'; break
                    if target is not None and highs[j] >= target:
                        exit_price, exit_reason = target, '목표'; break
                if exit_price is None:
                    exit_price, exit_reason = closes[min(i + HOLD_MAX, n - 1)], '기간만료'
                ret = ((exit_price / entry - 1) * 100 - COST_PCT) if entry else 0
                trades.append({'ret': ret, 'reason': exit_reason, 'bars': bars or 1})
                i += (bars or 1)        # 보유기간 동안 중복 진입 방지
            else:
                i += 1

        # ── 점수 구간별 미래수익 + '기준선' 대비 초과수익 ──
        # 기준선(base_fwd) = 아무 날이나(무작위 시점) 사서 20일 보유한 평균수익. 강세 종목은
        # 이 값이 크게 양수라 모든 점수구간이 그냥 플러스로 보인다(=점수가 좋아서가 아님).
        # 그래서 각 구간이 '기준선을 얼마나 넘었나(excess)'를 함께 본다 — excess가 0 근처면
        # 그 점수는 변별력이 없다는 뜻. 미래수익은 끝에서 HOLD_MAX일을 잘라 항상 '완전한
        # 20일 창'만 사용한다(끝부분 절단편향 제거 → 더 정확).
        BUCKETS = [(0, 40), (40, 55), (55, 68), (68, 80), (80, 101)]
        bkt = {f'{lo}-{hi if hi <= 100 else 100}': [] for lo, hi in BUCKETS}
        all_fwd = []
        for k in range(START, max(START, n - HOLD_MAX)):
            s = day_sig[k]['tech_score']
            fwd = (closes[k + HOLD_MAX] / closes[k] - 1) * 100 if closes[k] else 0
            all_fwd.append(fwd)
            for lo, hi in BUCKETS:
                if lo <= s < hi:
                    bkt[f'{lo}-{hi if hi <= 100 else 100}'].append(fwd)
                    break
        base_fwd = round(sum(all_fwd) / len(all_fwd), 2) if all_fwd else None
        score_buckets = [
            {'range': key,
             'n': len(v),
             'avg_fwd': round(sum(v) / len(v), 2) if v else None,
             'excess': round(sum(v) / len(v) - base_fwd, 2) if (v and base_fwd is not None) else None,
             'win_rate': round(sum(1 for x in v if x > 0) / len(v) * 100, 1) if v else None}
            for key, v in bkt.items()
        ]
        # 단조성: 점수가 오를수록 미래수익(=기준선 초과수익)도 오르는가. 1.0이면 완전 단조.
        avgs = [b['avg_fwd'] for b in score_buckets if b['avg_fwd'] is not None]
        monotonic = (round(sum(1 for a, b in zip(avgs, avgs[1:]) if b >= a) / (len(avgs) - 1), 2)
                     if len(avgs) >= 2 else None)

        total = len(trades)
        rets = [tr['ret'] for tr in trades]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        gp, gl = sum(wins), -sum(losses)
        # 복리 자본곡선 & 최대낙폭(MDD)
        eq, peak, mdd = 1.0, 1.0, 0.0
        for r in rets:
            eq *= (1 + r / 100)
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        bh = (closes[-1] / closes[START] - 1) * 100 - COST_PCT   # 단순보유(왕복비용 1회 차감)

        # 간이 아웃오브샘플: 거래를 시간순 전·후반으로 갈라 기대수익이 둘 다 양수인지(일관성).
        half = total // 2
        fh = [t['ret'] for t in trades[:half]]
        sh = [t['ret'] for t in trades[half:]]

        data = {
            'trades': total,
            'reliable': total >= 30,                                       # 표본 충분 여부(통계 신뢰)
            'win_rate': round(len(wins) / total * 100, 1) if total else None,
            'avg_return': round(sum(rets) / total, 2) if total else None,
            'avg_win': round(sum(wins) / len(wins), 2) if wins else None,
            'avg_loss': round(sum(losses) / len(losses), 2) if losses else None,
            'expectancy': round(sum(rets) / total, 2) if total else None,   # 1회 기대수익(%, 비용차감 순)
            'profit_factor': round(gp / gl, 2) if gl > 0 else None,
            'strategy_return': round((eq - 1) * 100, 1),                    # 전략 누적(복리, 순)
            'buy_hold_return': round(bh, 1),
            'max_drawdown': round(mdd * 100, 1),
            'base_fwd': base_fwd,                                           # 기준선: 무작위 시점 평균 미래수익
            'oos': {'first': round(sum(fh) / len(fh), 2) if fh else None,   # 전반 기대수익
                    'second': round(sum(sh) / len(sh), 2) if sh else None}, # 후반 기대수익
            'exits': {
                'target': sum(1 for tr in trades if tr['reason'] == '목표'),
                'stop':   sum(1 for tr in trades if tr['reason'] == '손절'),
                'time':   sum(1 for tr in trades if tr['reason'] == '기간만료'),
            },
            'params': {'buy_threshold': BUY_TH, 'max_hold_days': HOLD_MAX,
                       'period': BACKTEST_PERIOD, 'cost_pct': COST_PCT},
            'score_buckets': score_buckets,   # 점수 구간별 미래 20일 수익·기준선초과·승률
            'monotonic': monotonic,           # 인접 구간 우상향 비율 (1.0=완전 단조)
            'note': (f'왕복 매매비용 {COST_PCT}% 반영(수수료·세금·슬리피지 가정). '
                     '기준선=무작위 시점에 사서 20일 보유한 평균수익 — 각 점수구간이 이 기준선을 '
                     '넘어야(excess>0) 점수에 변별력이 있는 것. 거래 30건 미만은 표본 부족(참고용). '
                     '상대강도·주봉 필터는 백테스트 제외(중립). 가치 점수는 과거 데이터 제약으로 미검증. '
                     '과거 성과가 미래를 보장하지 않음.'),
        }
        _cache_set(('backtest', ticker), data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        log(f'backtest {ticker} 실패: {e}', 'ERROR')
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# 개인 동기화 코드(code)별로 그룹 데이터를 Supabase 테이블에 저장한다.
# Supabase 키는 서버 환경변수로만 보관하며 프론트엔드에 노출되지 않는다.
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')   # service_role 키 권장
SYNC_TABLE = 'watchlists'

def _sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
    }

def _sync_enabled():
    return bool(SUPABASE_URL and SUPABASE_KEY)

@app.route('/api/sync/<code>', methods=['GET'])
def sync_get(code):
    if not _sync_enabled():
        return jsonify({'success': False, 'error': '동기화가 서버에 설정되지 않았습니다'}), 503
    code = (code or '').strip()
    if not code:
        return jsonify({'success': False, 'error': '코드가 비어 있습니다'}), 400
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/{SYNC_TABLE}',
            headers=_sb_headers(),
            params={'code': f'eq.{code}', 'select': 'data', 'limit': '1'},
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json()
        return jsonify({'success': True, 'data': rows[0]['data'] if rows else None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/sync/<code>', methods=['PUT'])
def sync_put(code):
    if not _sync_enabled():
        return jsonify({'success': False, 'error': '동기화가 서버에 설정되지 않았습니다'}), 503
    code = (code or '').strip()
    if not code:
        return jsonify({'success': False, 'error': '코드가 비어 있습니다'}), 400
    payload = request.get_json(silent=True) or {}
    data = payload.get('data')
    try:
        # code를 기준으로 upsert (있으면 갱신, 없으면 삽입)
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/{SYNC_TABLE}',
            headers={**_sb_headers(), 'Prefer': 'resolution=merge-duplicates'},
            json=[{'code': code, 'data': data}],
            timeout=10,
        )
        r.raise_for_status()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# keep-alive 핑/상태 점검용 — 외부 의존(yfinance·Supabase) 없이 즉시 응답한다.
# Render 무료 플랜의 spin-down을 막으려 외부 cron이 주기적으로 호출하는 용도.
@app.route('/api/health')
def health():
    return jsonify({'ok': True})


# 프론트엔드(index.html) 서빙 — API와 같은 서버에서 제공
@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(__file__), 'index.html'))


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('PORT', '5000'))
    app.run(debug=debug, host='0.0.0.0', port=port)
