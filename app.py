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
    PER/PBR 계산.
    yfinance 제공값 우선, 없으면 분기 재무제표로 TTM 직접 계산.
    TTM EPS = 최근 4개 분기 순이익 합 / 발행주식수
    BPS     = 최근 분기 자기자본 / 발행주식수
    """
    per = safe_val(info.get('trailingPE'))
    pbr = safe_val(info.get('priceToBook'))
    eps = safe_val(info.get('trailingEps'))
    bps = safe_val(info.get('bookValue'))
    price = safe_val(info.get('currentPrice') or info.get('regularMarketPrice'))
    shares = safe_val(info.get('sharesOutstanding') or info.get('impliedSharesOutstanding'))

    if (per is None or pbr is None) and shares and price:
        try:
            NI_KEYS = ('Net Income From Continuing And Discontinued Operation',
                       'Net Income', 'Net Income Common Stockholders')
            EQ_KEYS = ('Common Stock Equity', 'Stockholders Equity',
                       'Total Equity Gross Minority Interest')

            # 분기 재무제표로 TTM 계산
            qfin = t.quarterly_financials
            qbs  = t.quarterly_balance_sheet

            if eps is None and qfin is not None and not qfin.empty:
                ni_key = next((k for k in NI_KEYS if k in qfin.index), None)
                if ni_key:
                    ttm_ni = safe_val(qfin.loc[ni_key].iloc[:4].sum())
                    if ttm_ni and shares:
                        eps = ttm_ni / shares

            if bps is None and qbs is not None and not qbs.empty:
                eq_key = next((k for k in EQ_KEYS if k in qbs.index), None)
                if eq_key:
                    eq = safe_val(qbs.loc[eq_key].iloc[0])
                    if eq and shares:
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
            'name': info.get('longName') or info.get('shortName') or ticker.upper(),
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


# 프론트엔드(index.html) 서빙 — API와 같은 서버에서 제공
@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(__file__), 'index.html'))


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('PORT', '5001'))
    app.run(debug=debug, host='0.0.0.0', port=port)
