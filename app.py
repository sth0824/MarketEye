from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yfinance as yf
import requests
import traceback
import json
import io
import os
import time
import threading
import unicodedata

app = Flask(__name__)
CORS(app)

# ── 간단한 TTL 캐시 (Yahoo 레이트리밋 방지) ──────────────
_cache = {}
_cache_lock = threading.Lock()

def _cache_get(key, ttl):
    with _cache_lock:
        e = _cache.get(key)
        if e and time.time() - e[0] < ttl:
            return e[1]
    return None

def _cache_set(key, val):
    with _cache_lock:
        _cache[key] = (time.time(), val)

# ── KRX 전체 상장 종목 캐시 ──────────────────────────────
_krx_stocks = []   # [{'code': '005930', 'ticker': '005930.KS', 'name': '삼성전자', 'market': 'KOSPI'}]

def _load_krx():
    """KRX 상장 종목 전체를 가져와 캐싱 (KOSPI + KOSDAQ 통합)"""
    global _krx_stocks
    try:
        from html.parser import HTMLParser
        url = 'https://kind.krx.co.kr/corpgeneral/corpList.do'
        params = {'method': 'download', 'searchType': '13'}
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://kind.krx.co.kr/'}
        res = requests.get(url, params=params, headers=headers, timeout=15)
        res.encoding = 'euc-kr'

        class TblParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_cell = False
                self.rows, self.cur, self.cell = [], [], ''
            def handle_starttag(self, tag, attrs):
                if tag == 'tr': self.cur = []
                elif tag in ('td', 'th'): self.in_cell = True; self.cell = ''
            def handle_endtag(self, tag):
                if tag in ('td', 'th'):
                    self.in_cell = False
                    self.cur.append(self.cell.strip())
                elif tag == 'tr':
                    if self.cur: self.rows.append(self.cur)
            def handle_data(self, data):
                if self.in_cell: self.cell += data.strip()

        p = TblParser()
        p.feed(res.text)
        rows = p.rows
        if not rows:
            print('KRX: no rows parsed')
            return

        header = rows[0]
        code_idx = next((i for i,h in enumerate(header) if '종목코드' in h), None)
        name_idx = next((i for i,h in enumerate(header) if '회사명' in h), None)
        mkt_idx  = next((i for i,h in enumerate(header) if '시장구분' in h), None)

        if code_idx is None or name_idx is None:
            print('KRX: header parse failed', header)
            return

        results = []
        for row in rows[1:]:
            if len(row) <= max(code_idx, name_idx): continue
            code = row[code_idx].strip().zfill(6)
            name = row[name_idx].strip()
            mkt_raw = row[mkt_idx].strip() if mkt_idx is not None else ''
            if not code or not name or len(code) > 7: continue
            # 코스닥이면 .KQ, 나머지(코스피 등)는 .KS
            suffix = 'KQ' if '코스닥' in mkt_raw else 'KS'
            market = 'KOSDAQ' if suffix == 'KQ' else 'KOSPI'
            results.append({'code': code, 'ticker': f'{code}.{suffix}', 'name': name, 'market': market})

        if not results:
            print('KRX: parsed 0 valid stocks, keeping existing data')
            return
        _krx_stocks = results
        print(f'KRX stocks loaded: {len(_krx_stocks)}')
    except Exception as e:
        print('KRX load error:', e)
        traceback.print_exc()

def _normalize(s):
    """검색용 정규화: 소문자 + 공백/특수문자 제거"""
    return unicodedata.normalize('NFC', s).lower().replace(' ', '').replace('(', '').replace(')', '').replace(',', '')

def _search_krx(query, limit=10):
    q = _normalize(query)
    exact, starts, contains = [], [], []
    for s in _krx_stocks:
        n = _normalize(s['name'])
        c = s['code']
        if n == q or c == q or s['ticker'].lower() == q:
            exact.append(s)
        elif n.startswith(q) or c.startswith(q):
            starts.append(s)
        elif q in n:
            contains.append(s)
    return (exact + starts + contains)[:limit]

def _krx_name(ticker):
    """티커에 해당하는 KRX 한글 종목명을 반환 (없으면 None)."""
    tl = (ticker or '').lower()
    for s in _krx_stocks:
        if s['ticker'].lower() == tl:
            return s['name']
    return None

def _load_krx_bundle():
    """저장소에 포함된 krx_stocks.json을 로드 (KRX 사이트 접근 불가한 환경용 폴백)."""
    global _krx_stocks
    try:
        path = os.path.join(os.path.dirname(__file__), 'krx_stocks.json')
        with open(path, encoding='utf-8') as f:
            _krx_stocks = json.load(f)
        print(f'KRX bundle loaded: {len(_krx_stocks)}')
        return True
    except Exception as e:
        print('KRX bundle load failed:', e)
        return False

# 앱 시작 시 KRX 로드: 번들 먼저(즉시 검색 가능) → 라이브 갱신 시도(되면 최신화)
def _krx_loader():
    _load_krx_bundle()           # 항상 동작하는 기본값 (해외 IP에서도 OK)
    try:
        _load_krx()              # KRX 사이트 접근 가능하면 최신 목록으로 교체
    except Exception:
        pass                     # 실패해도 번들 데이터 유지

threading.Thread(target=_krx_loader, daemon=True).start()


def safe_val(val):
    if val is None:
        return None
    try:
        if hasattr(val, 'item'):
            val = val.item()
        f = float(val)
        if f != f:  # NaN check
            return None
        return f
    except (TypeError, ValueError):
        return None


def _yahoo_search(query):
    url = 'https://query1.finance.yahoo.com/v1/finance/search'
    params = {'q': query, 'lang': 'en-US', 'region': 'US',
              'quotesCount': 10, 'newsCount': 0, 'enableFuzzyQuery': True, 'enableCb': False}
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    res = requests.get(url, params=params, headers=headers, timeout=5)
    return res.json().get('quotes', [])


def _is_korean(s):
    return any('가' <= c <= '힣' or 'ᄀ' <= c <= 'ᇿ' for c in s)


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

        # 2차: 한국어가 아니면 야후 글로벌 검색으로 해외 종목 추가
        if not _is_korean(query) and len(results) < 10:
            for q in _yahoo_search(query):
                qtype = q.get('quoteType', '')
                if qtype not in ('EQUITY', 'ETF', 'INDEX'):
                    continue
                add(q.get('symbol',''), q.get('longname') or q.get('shortname') or '', q.get('exchange',''), qtype)

        return jsonify(results[:10])
    except Exception as e:
        traceback.print_exc()
        return jsonify([])


def _calc_per_pbr(info, t):
    """
    PER/PBR 계산. yfinance 제공값 우선, 없으면 분기 재무제표로 TTM 직접 계산.

    EPS(TTM) 우선순위:
      1) info['trailingEps'] (야후 제공)
      2) 분기 EPS(Diluted→Basic) 최근 4개 합 — 가중평균·희석이 이미 반영돼 가장 정확
      3) 최근 4개 분기 '지배주주 순이익' 합 ÷ 발행주식수 (근사)
    BPS:
      1) info['bookValue']
      2) 최근 분기 보통주 자기자본 ÷ 발행주식수
    정확도를 위해 분기가 4개 미만이거나 결측이면 계산하지 않고 None을 반환한다.
    """
    per = safe_val(info.get('trailingPE'))
    pbr = safe_val(info.get('priceToBook'))
    eps = safe_val(info.get('trailingEps'))
    bps = safe_val(info.get('bookValue'))
    price = safe_val(info.get('currentPrice') or info.get('regularMarketPrice'))
    shares = safe_val(info.get('sharesOutstanding') or info.get('impliedSharesOutstanding'))

    if (per is None or pbr is None) and price:
        try:
            # 분기 EPS 직접 합산용 (가장 정확). 보통주 귀속 EPS.
            EPS_KEYS = ('Diluted EPS', 'Basic EPS')
            # 순이익 폴백: 지배주주(보통주) 귀속분을 우선해 EPS 정의에 맞춤
            NI_KEYS = ('Net Income Common Stockholders',
                       'Net Income From Continuing And Discontinued Operation',
                       'Net Income')
            EQ_KEYS = ('Common Stock Equity', 'Stockholders Equity',
                       'Total Equity Gross Minority Interest')

            # 분기 재무제표로 TTM 계산
            qfin = t.quarterly_financials
            qbs  = t.quarterly_balance_sheet

            if eps is None and qfin is not None and not qfin.empty:
                # 1순위: 분기 EPS 4개 합 (min_count=4 → 4분기 모두 있어야 계산)
                eps_key = next((k for k in EPS_KEYS if k in qfin.index), None)
                if eps_key:
                    eps = safe_val(qfin.loc[eps_key].iloc[:4].sum(min_count=4))
                # 2순위: 지배주주 순이익 TTM ÷ 발행주식수 (분기 EPS가 없을 때만)
                if eps is None and shares:
                    ni_key = next((k for k in NI_KEYS if k in qfin.index), None)
                    if ni_key:
                        ttm_ni = safe_val(qfin.loc[ni_key].iloc[:4].sum(min_count=4))
                        if ttm_ni is not None:
                            eps = ttm_ni / shares

            if bps is None and shares and qbs is not None and not qbs.empty:
                eq_key = next((k for k in EQ_KEYS if k in qbs.index), None)
                if eq_key:
                    eq = safe_val(qbs.loc[eq_key].iloc[0])
                    if eq:
                        bps = eq / shares

        except Exception:
            pass

    if per is None and price and eps and eps > 0:
        per = price / eps
    if pbr is None and price and bps and bps > 0:
        pbr = price / bps

    return per, pbr, eps, bps


def _fetch_stock(ticker):
    """yfinance에서 종목 데이터를 조회해 dict로 반환 (실패 시 예외)."""
    t = yf.Ticker(ticker.upper())
    info = t.info

    # fast_info로 실시간에 가까운 가격 보완 (price 계산 전에 먼저 반영)
    try:
        fi = t.fast_info
        if fi.last_price:
            info['currentPrice'] = fi.last_price
        if fi.previous_close:
            info['regularMarketPreviousClose'] = fi.previous_close
    except Exception:
        pass

    # 현재가 (fast_info 보완 후 계산)
    price = safe_val(info.get('currentPrice') or info.get('regularMarketPrice'))

    # 52주 범위
    low52 = safe_val(info.get('fiftyTwoWeekLow'))
    high52 = safe_val(info.get('fiftyTwoWeekHigh'))

    # PER / PBR 계산 (yfinance 미제공 시 분기 TTM 재무제표로 직접 계산)
    per, pbr, eps_calc, bps_calc = _calc_per_pbr(info, t)

    # 배당수익률: 최신 yfinance는 퍼센트 숫자(0.36 = 0.36%)로 반환 → 비율(fraction)로 정규화
    div_yield = safe_val(info.get('dividendYield'))
    if div_yield is not None:
        div_yield = div_yield / 100

    # 최근 1년 종가 히스토리 (차트용 - 월별)
    hist = t.history(period='1y', interval='1mo')
    history = []
    if not hist.empty:
        for dt, row in hist.iterrows():
            history.append({
                'date': dt.strftime('%Y-%m'),
                'close': round(float(row['Close']), 2)
            })

    data = {
            'ticker': ticker.upper(),
            'name': _krx_name(ticker) or info.get('longName') or info.get('shortName') or ticker.upper(),
            'sector': info.get('sector', '-'),
            'industry': info.get('industry', '-'),
            'currency': info.get('currency', 'USD'),
            'exchange': info.get('exchange', '-'),

            # 가격
            'price': price,
            'change': safe_val(info.get('regularMarketChange')),
            'changePercent': safe_val(info.get('regularMarketChangePercent')),
            'open': safe_val(info.get('regularMarketOpen')),
            'prevClose': safe_val(info.get('regularMarketPreviousClose')),
            'dayLow': safe_val(info.get('regularMarketDayLow')),
            'dayHigh': safe_val(info.get('regularMarketDayHigh')),
            'low52': low52,
            'high52': high52,
            'volume': safe_val(info.get('regularMarketVolume')),
            'avgVolume': safe_val(info.get('averageVolume')),

            # 밸류에이션
            'per': per,
            'forwardPer': safe_val(info.get('forwardPE')),
            'pbr': pbr,
            'psr': safe_val(info.get('priceToSalesTrailing12Months')),
            'evEbitda': safe_val(info.get('enterpriseToEbitda')),
            'evRevenue': safe_val(info.get('enterpriseToRevenue')),

            # 수익성
            'roe': safe_val(info.get('returnOnEquity')),
            'roa': safe_val(info.get('returnOnAssets')),
            'grossMargin': safe_val(info.get('grossMargins')),
            'operatingMargin': safe_val(info.get('operatingMargins')),
            'netMargin': safe_val(info.get('profitMargins')),
            'ebitdaMargin': safe_val(info.get('ebitdaMargins')),

            # 성장성
            'revenueGrowth': safe_val(info.get('revenueGrowth')),
            'earningsGrowth': safe_val(info.get('earningsGrowth')),
            'earningsQuarterlyGrowth': safe_val(info.get('earningsQuarterlyGrowth')),

            # 재무 건전성
            'debtToEquity': safe_val(info.get('debtToEquity')),
            'currentRatio': safe_val(info.get('currentRatio')),
            'quickRatio': safe_val(info.get('quickRatio')),
            'totalCash': safe_val(info.get('totalCash')),
            'totalDebt': safe_val(info.get('totalDebt')),
            'freeCashflow': safe_val(info.get('freeCashflow')),

            # 규모
            'marketCap': safe_val(info.get('marketCap')),
            'enterpriseValue': safe_val(info.get('enterpriseValue')),
            'revenue': safe_val(info.get('totalRevenue')),
            'ebitda': safe_val(info.get('ebitda')),
            'eps': eps_calc or safe_val(info.get('trailingEps')),
            'forwardEps': safe_val(info.get('forwardEps')),
            'bookValue': bps_calc or safe_val(info.get('bookValue')),

            # 배당
            'dividendYield': div_yield,
            'dividendRate': safe_val(info.get('dividendRate')),
            'payoutRatio': safe_val(info.get('payoutRatio')),
            'exDividendDate': info.get('exDividendDate'),

            # 애널리스트
            'targetPrice': safe_val(info.get('targetMeanPrice')),
            'targetLow': safe_val(info.get('targetLowPrice')),
            'targetHigh': safe_val(info.get('targetHighPrice')),
            'recommendationMean': safe_val(info.get('recommendationMean')),
            'recommendation': info.get('recommendationKey', '-'),
            'numberOfAnalysts': info.get('numberOfAnalystOpinions'),

            # 기타
            'beta': safe_val(info.get('beta')),
            'sharesOutstanding': safe_val(info.get('sharesOutstanding')),
            'floatShares': safe_val(info.get('floatShares')),
            'shortRatio': safe_val(info.get('shortRatio')),

            'history': history,
    }
    return data


# 전체 데이터 TTL (초). 환경변수로 조정 가능.
STOCK_TTL = int(os.environ.get('STOCK_TTL', '60'))
PRICE_TTL = int(os.environ.get('PRICE_TTL', '15'))


@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    key = ticker.upper()
    cached = _cache_get(('stock', key), STOCK_TTL)
    if cached is not None:
        return jsonify({'success': True, 'data': cached})
    try:
        data = _fetch_stock(ticker)
        _cache_set(('stock', key), data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/price/<ticker>')
def get_price(ticker):
    """가격·등락률만 빠르게 반환 (fast_info). 자동 새로고침용 경량 엔드포인트."""
    key = ticker.upper()
    cached = _cache_get(('price', key), PRICE_TTL)
    if cached is not None:
        return jsonify({'success': True, 'data': cached})
    try:
        t = yf.Ticker(key)
        fi = t.fast_info
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
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/batch')
def get_batch():
    """여러 종목을 병렬로 조회."""
    tickers = request.args.get('tickers', '')
    ticker_list = [t.strip() for t in tickers.split(',') if t.strip()]
    results = {}
    lock = threading.Lock()

    def work(ticker):
        key = ticker.upper()
        cached = _cache_get(('stock', key), STOCK_TTL)
        try:
            data = cached if cached is not None else _fetch_stock(ticker)
            if cached is None:
                _cache_set(('stock', key), data)
            payload = {'success': True, 'data': data}
        except Exception as e:
            payload = {'success': False, 'error': str(e)}
        with lock:
            results[key] = payload

    threads = [threading.Thread(target=work, args=(t,)) for t in ticker_list]
    for th in threads: th.start()
    for th in threads: th.join()
    return jsonify(results)


# ── 진입 시점 신호 분석 ─────────────────────────────────────
def _ema(data, period):
    k = 2 / (period + 1)
    result = [data[0]]
    for v in data[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def _rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return 50.0
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

@app.route('/api/signal/<path:ticker>')
def signal(ticker):
    cached = _cache_get(('signal', ticker), 1800)
    if cached:
        return jsonify({'success': True, 'data': cached})
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period='1y', interval='1d')
        if hist.empty or len(hist) < 30:
            return jsonify({'success': False, 'error': '데이터 부족 (최소 30일 필요)'}), 422

        closes = [float(v) for v in hist['Close'].tolist()]
        price = closes[-1]

        # ── RSI(14) ──
        rsi = _rsi(closes, 14)
        if rsi < 30:   rsi_s = 90
        elif rsi < 40: rsi_s = 75
        elif rsi < 50: rsi_s = 58
        elif rsi < 60: rsi_s = 45
        elif rsi < 70: rsi_s = 28
        else:          rsi_s = 12

        # ── 이동평균 ──
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
        if ma20 and ma60:
            if price > ma20 > ma60:   ma_s = 80  # 정배열
            elif price > ma20:        ma_s = 65
            elif price < ma20 < ma60: ma_s = 20  # 역배열
            else:                     ma_s = 35
        else:
            ma_s = 50

        # ── MACD(12,26,9) ──
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        macd_line = [a - b for a, b in zip(ema12, ema26)]
        sig_line  = _ema(macd_line, 9)
        macd_val, sig_val = macd_line[-1], sig_line[-1]
        macd_cross = macd_val > sig_val and macd_line[-2] <= sig_line[-2]
        macd_s = 85 if macd_cross else (62 if macd_val > sig_val else 38)

        # ── 볼린저 밴드(20,2) ──
        if len(closes) >= 20:
            r20 = closes[-20:]
            bb_mid = sum(r20) / 20
            std = (sum((x - bb_mid) ** 2 for x in r20) / 20) ** 0.5
            bb_upper = bb_mid + 2 * std
            bb_lower = bb_mid - 2 * std
            span = bb_upper - bb_lower
            bb_pct = (price - bb_lower) / span if span > 0 else 0.5
        else:
            bb_mid = bb_upper = bb_lower = None
            bb_pct = 0.5
        if bb_pct < 0.10:   bb_s = 90
        elif bb_pct < 0.25: bb_s = 75
        elif bb_pct < 0.50: bb_s = 55
        elif bb_pct < 0.75: bb_s = 42
        elif bb_pct < 0.90: bb_s = 28
        else:               bb_s = 12

        tech_score = round(rsi_s * 0.35 + ma_s * 0.30 + macd_s * 0.20 + bb_s * 0.15)

        # ── 펀더멘털 ──
        info = t.info
        def sv(k): return info.get(k)

        def s_per(v):
            if v is None: return 50
            if v < 0: return 20
            if v < 10: return 90;
            if v < 15: return 80
            if v < 20: return 65
            if v < 30: return 45
            return 20

        def s_pbr(v):
            if v is None: return 50
            if v < 1: return 90
            if v < 2: return 75
            if v < 3: return 55
            return 25

        def s_roe(v):
            if v is None: return 50
            p = v * 100
            if p > 25: return 95
            if p > 15: return 80
            if p > 10: return 65
            if p > 5:  return 45
            return 20

        def s_debt(v):
            if v is None: return 50
            r = v / 100
            if r < 0.5: return 90
            if r < 1.0: return 75
            if r < 2.0: return 55
            return 25

        def s_margin(v):
            if v is None: return 50
            p = v * 100
            if p > 20: return 90
            if p > 10: return 75
            if p > 5:  return 55
            return 25

        # PER/PBR: yfinance 미제공(한국 종목 등) 시 분기 재무제표로 TTM 직접 계산
        # — 개요 카드와 동일한 폴백 로직을 재사용해 '-'로 비는 문제를 막는다.
        per, pbr, _, _ = _calc_per_pbr(info, t)
        roe = sv('returnOnEquity'); debt = sv('debtToEquity')
        margin = sv('profitMargins')

        fund_score = round(
            s_per(per)    * 0.30 +
            s_pbr(pbr)    * 0.20 +
            s_roe(roe)    * 0.25 +
            s_debt(debt)  * 0.10 +
            s_margin(margin) * 0.15
        )

        combined = round(tech_score * 0.55 + fund_score * 0.45)

        if combined >= 68:   label = '매수 고려'
        elif combined >= 48: label = '관망'
        else:                label = '주의'

        data = {
            'combined': combined,
            'tech_score': tech_score,
            'fund_score': fund_score,
            'signal': label,
            'indicators': {
                'rsi': round(rsi, 1),
                'ma20': round(ma20, 2) if ma20 else None,
                'ma60': round(ma60, 2) if ma60 else None,
                'price': round(price, 2),
                'macd': round(macd_val, 4),
                'macd_signal': round(sig_val, 4),
                'macd_cross': macd_cross,
                'bb_upper': round(bb_upper, 2) if bb_upper else None,
                'bb_mid':   round(bb_mid, 2)   if bb_mid   else None,
                'bb_lower': round(bb_lower, 2) if bb_lower else None,
                'bb_pct': round(bb_pct * 100, 1),
            },
            'fundamentals': {
                'per': per, 'pbr': pbr, 'roe': roe,
                'debt': debt, 'margin': margin,
            }
        }
        _cache_set(('signal', ticker), data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
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


# 프론트엔드(index.html) 서빙 — API와 같은 서버에서 제공
@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(__file__), 'index.html'))


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('PORT', '5001'))
    app.run(debug=debug, host='0.0.0.0', port=port)
