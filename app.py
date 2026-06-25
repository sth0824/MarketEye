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
import re

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


# ── 네이버 증권 실시간 시세 (한국 종목; 야후 KRX 15~20분 지연 해소) ──────────
_NAVER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36',
    'Referer': 'https://m.stock.naver.com/',
}


def _naver_num(s):
    """'358,000' · '5.14' · '28.90배' · '0.47%' · '12,372원' 등을 float로 정규화."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    cleaned = re.sub(r'[^0-9.\-]', '', str(s))
    if cleaned in ('', '-', '.', '-.'):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _naver_won(s):
    """'2,090조 446억' 형태의 시가총액을 원 단위 float로 변환."""
    if s is None:
        return None
    txt = str(s)
    total = 0.0
    matched = False
    for unit, mul in (('조', 1e12), ('억', 1e8), ('만', 1e4)):
        m = re.search(r'([\d,]+)\s*' + unit, txt)
        if m:
            total += float(m.group(1).replace(',', '')) * mul
            matched = True
    return total if matched else _naver_num(txt)


def _fetch_naver(code):
    """네이버 증권 모바일 JSON에서 실시간 시세·펀더멘털을 dict로 반환.
    code: 종목코드 6자리(예: '005930'). 실패 시 예외를 던진다."""
    out = {}

    # 1) 실시간 시세 (현재가·등락) — basic 엔드포인트
    b = requests.get(f'https://m.stock.naver.com/api/stock/{code}/basic',
                     headers=_NAVER_HEADERS, timeout=5).json()
    price = _naver_num(b.get('closePrice'))
    chg = _naver_num(b.get('compareToPreviousClosePrice'))
    pct = _naver_num(b.get('fluctuationsRatio'))
    out['tradeDate'] = ((b.get('localTradedAt') or '')[:10]) or None  # 'YYYY-MM-DD'
    dir_code = str((b.get('compareToPreviousPrice') or {}).get('code') or '')
    if price is not None and chg is not None and pct is not None:
        # 네이버 방향코드: 1·2=상승(+), 3=보합(0), 4·5=하락(-)
        sign = -1 if dir_code in ('4', '5') else (0 if dir_code == '3' else 1)
        out['price'] = price
        out['change'] = sign * chg
        out['changePercent'] = sign * pct
        out['prevClose'] = price - sign * chg

    # 2) 펀더멘털 + 당일 OHLC·52주 — integration 엔드포인트
    i = requests.get(f'https://m.stock.naver.com/api/stock/{code}/integration',
                     headers=_NAVER_HEADERS, timeout=5).json()
    ti = {x.get('code'): x.get('value') for x in (i.get('totalInfos') or [])}
    out['open'] = _naver_num(ti.get('openPrice'))
    out['dayHigh'] = _naver_num(ti.get('highPrice'))
    out['dayLow'] = _naver_num(ti.get('lowPrice'))
    out['volume'] = _naver_num(ti.get('accumulatedTradingVolume'))
    out['high52'] = _naver_num(ti.get('highPriceOf52Weeks'))
    out['low52'] = _naver_num(ti.get('lowPriceOf52Weeks'))
    out['per'] = _naver_num(ti.get('per'))
    out['forwardPer'] = _naver_num(ti.get('cnsPer'))
    out['eps'] = _naver_num(ti.get('eps'))
    out['forwardEps'] = _naver_num(ti.get('cnsEps'))
    out['pbr'] = _naver_num(ti.get('pbr'))
    out['bookValue'] = _naver_num(ti.get('bps'))
    dy = _naver_num(ti.get('dividendYieldRatio'))
    out['dividendYield'] = dy / 100 if dy is not None else None
    out['dividendRate'] = _naver_num(ti.get('dividend'))
    out['marketCap'] = _naver_won(ti.get('marketValue'))
    return out


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

    # 등락률: 가격을 fast_info로 최신화했으므로 등락도 같은 기준(최신가-전일종가)으로
    # 재계산해 표시 가격과 일치시킨다. 전일종가가 없으면 야후 제공값을 사용.
    prev_close = safe_val(info.get('regularMarketPreviousClose'))
    change = safe_val(info.get('regularMarketChange'))
    change_pct = safe_val(info.get('regularMarketChangePercent'))
    if price is not None and prev_close:
        change = price - prev_close
        change_pct = change / prev_close * 100

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
            'change': change,
            'changePercent': change_pct,
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

    # 한국 종목은 네이버 실시간 시세·펀더멘털로 보강 (야후 KRX 15~20분 지연 해소).
    # 네이버 조회 실패 시 위에서 만든 야후 값을 그대로 사용한다(폴백).
    tk = ticker.upper()
    if tk.endswith('.KS') or tk.endswith('.KQ'):
        try:
            nv = _fetch_naver(tk.split('.')[0])
            for k, v in nv.items():
                if v is not None:
                    data[k] = v
        except Exception:
            pass

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

def _sma(arr, period, off=0):
    """끝에서 off개 떨어진 지점 기준 단순이동평균. 데이터 부족 시 None."""
    end = len(arr) - off
    if end < period:
        return None
    return sum(arr[end - period:end]) / period

def _rsi_series(closes, period=14):
    """각 시점의 RSI를 리스트로 반환 (warmup 구간은 None). 다이버전스 탐지용."""
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, n)]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

def _atr(highs, lows, closes, period=14):
    """평균 진폭(ATR, Wilder). 손절폭·변동성 산정용. 데이터 부족 시 None."""
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr

def _pivots(highs, lows, k=5):
    """프랙탈 스윙 고점/저점 탐지. (스윙고점가 리스트, 스윙저점가 리스트) 반환.
    인덱스 i가 좌우 k개보다 높으면(낮으면) 스윙 고점(저점)."""
    n = len(highs)
    ph, pl = [], []
    for i in range(k, n - k):
        win_h = highs[i - k:i + k + 1]
        win_l = lows[i - k:i + k + 1]
        if highs[i] == max(win_h):
            ph.append((i, highs[i]))
        if lows[i] == min(win_l):
            pl.append((i, lows[i]))
    return ph, pl

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
        h = yf.Ticker(symbol).history(period='1y', interval='1d')
        closes = [float(v) for v in h['Close'].tolist()] if not h.empty else []
    except Exception:
        closes = []
    _cache_set(('bench', symbol), closes)
    return closes

def _technical_signal(closes, highs, lows, vols, rs_60=None, weekly_up=None, with_reasons=True):
    """가격/거래량 배열만으로 기술적 매수 점수를 계산 (네트워크·펀더멘털 불필요).
    배열의 '마지막 시점' 기준으로 평가 → 실시간 신호와 백테스트가 동일 로직을 공유한다.
    rs_60(시장 대비 60일 초과수익)·weekly_up(주봉 추세)은 선택 입력, 없으면 중립 처리."""
    n = len(closes)
    price = closes[-1]

    # 1단계: 추세 판정 (모든 매수신호의 대전제)
    ma20  = _sma(closes, 20)
    ma60  = _sma(closes, 60)
    ma120 = _sma(closes, 120)
    ma200 = _sma(closes, 200)
    lt = ma200 or ma120 or ma60
    ma60_prev = _sma(closes, 60, 20)
    slope60 = (ma60 - ma60_prev) if (ma60 and ma60_prev) else 0.0
    rising60 = slope60 > 0
    above_lt = bool(lt and price > lt)
    align_up = bool(ma20 and ma60 and price > ma20 > ma60 and (not ma120 or ma60 > ma120))
    align_dn = bool(ma20 and ma60 and price < ma20 < ma60 and (not ma120 or ma60 < ma120))
    if align_up and above_lt and rising60:
        regime, regime_label, trend_s = 'strong_up', '강한 상승추세', 90
    elif above_lt and rising60:
        regime, regime_label, trend_s = 'up', '상승추세', 72
    elif align_dn and not above_lt and not rising60:
        regime, regime_label, trend_s = 'strong_down', '강한 하락추세', 8
    elif (not above_lt) and not rising60:
        regime, regime_label, trend_s = 'down', '하락추세', 22
    else:
        regime, regime_label, trend_s = 'range', '횡보', 50
    is_up   = regime in ('up', 'strong_up')
    is_down = regime in ('down', 'strong_down')

    # 2단계: RSI(14) + 강세 다이버전스
    rsi_arr = _rsi_series(closes, 14)
    rsi = rsi_arr[-1] if rsi_arr[-1] is not None else 50.0
    bull_div = False
    if n >= 35 and all(x is not None for x in rsi_arr[-30:]):
        recent, prior = closes[-12:], closes[-30:-12]
        ri = n - 12 + recent.index(min(recent))
        pi = n - 30 + prior.index(min(prior))
        if closes[ri] < closes[pi] and rsi_arr[ri] > rsi_arr[pi] + 2:
            bull_div = True
    if is_up:
        if   rsi < 40: rsi_s = 78
        elif rsi < 55: rsi_s = 88
        elif rsi < 70: rsi_s = 60
        else:          rsi_s = 32
    elif is_down:
        if   rsi < 30: rsi_s = 25
        elif rsi < 50: rsi_s = 32
        else:          rsi_s = 38
    else:
        if   rsi < 30: rsi_s = 88
        elif rsi < 45: rsi_s = 68
        elif rsi < 55: rsi_s = 50
        elif rsi < 70: rsi_s = 38
        else:          rsi_s = 18
    if bull_div:
        rsi_s = min(100, rsi_s + 15)

    # 3단계: MACD(12,26,9) — 제로라인·히스토그램 모멘텀
    ema12 = _ema(closes, 12); ema26 = _ema(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    sig_line  = _ema(macd_line, 9)
    macd_val, sig_val = macd_line[-1], sig_line[-1]
    macd_cross = macd_val > sig_val and macd_line[-2] <= sig_line[-2]
    hist_rising = (macd_val - sig_val) > (macd_line[-2] - sig_line[-2])
    if macd_cross:
        macd_s = 92 if macd_val > 0 else 82
    elif macd_val > sig_val:
        macd_s = (72 if macd_val > 0 else 60) + (4 if hist_rising else -6)
    else:
        macd_s = (46 if macd_val > 0 else 30) + (6 if hist_rising else 0)
    macd_s = max(0, min(100, macd_s))

    # 4단계: 볼린저 밴드(20,2) — 추세 맥락 반영
    r20 = closes[-20:]
    bb_mid = sum(r20) / 20
    std = (sum((x - bb_mid) ** 2 for x in r20) / 20) ** 0.5
    bb_upper, bb_lower = bb_mid + 2 * std, bb_mid - 2 * std
    span = bb_upper - bb_lower
    bb_pct = (price - bb_lower) / span if span > 0 else 0.5
    if is_down:
        bb_s = 30 if bb_pct < 0.25 else (40 if bb_pct < 0.75 else 25)
    else:
        if   bb_pct < 0.15: bb_s = 85
        elif bb_pct < 0.40: bb_s = 65
        elif bb_pct < 0.65: bb_s = 52
        elif bb_pct < 0.90: bb_s = 38
        else:               bb_s = 22
    if bull_div and bb_pct < 0.3:
        bb_s = min(100, bb_s + 8)

    # 5단계: 거래량 확인
    vol_ma20 = (sum(vols[-20:]) / 20) if (len(vols) >= 20 and sum(vols[-20:]) > 0) else None
    vol_ratio = (vols[-1] / vol_ma20) if vol_ma20 else 1.0
    up_bar = closes[-1] >= closes[-2]
    if up_bar:
        vol_s = 88 if vol_ratio >= 1.8 else (68 if vol_ratio >= 1.1 else 50)
    else:
        vol_s = 22 if vol_ratio >= 1.8 else (38 if vol_ratio >= 1.1 else 48)

    # 6단계: 상대강도(입력) · 시장구조 · 매매플랜
    rs_s = 50
    if rs_60 is not None:
        if   rs_60 > 0.10:  rs_s = 92
        elif rs_60 > 0.03:  rs_s = 76
        elif rs_60 > -0.03: rs_s = 55
        elif rs_60 > -0.10: rs_s = 38
        else:               rs_s = 20
    weekly_label = '-' if weekly_up is None else ('상승' if weekly_up else '하락/횡보')

    structure_s, structure_label = 50, '불명확'
    support = resistance = None
    try:
        ph, pl = _pivots(highs, lows, 5)
        if len(ph) >= 2 and len(pl) >= 2:
            hh, hl = ph[-1][1] > ph[-2][1], pl[-1][1] > pl[-2][1]
            if hh and hl:               structure_s, structure_label = 85, '상승구조(HH·HL)'
            elif (not hh) and (not hl): structure_s, structure_label = 20, '하락구조(LH·LL)'
            else:                       structure_s, structure_label = 50, '전환/혼조'
        below = [p for _, p in pl if p < price]
        above = [p for _, p in ph if p > price]
        support = max(below) if below else None
        resistance = min(above) if above else None
    except Exception:
        pass

    atr = stop = target = rr = stop_pct = target_pct = None
    try:
        atr = _atr(highs, lows, closes, 14)
        if atr and atr > 0:
            raw_stop = (support * 0.99) if (support and support < price) else (price - 2 * atr)
            stop = raw_stop if raw_stop < price else (price - 2 * atr)
            risk = price - stop
            if risk > 0:
                target = resistance if (resistance and resistance > price) else (price + 2 * risk)
                if target <= price:
                    target = price + 2 * risk
                rr = (target - price) / risk
                stop_pct = (price - stop) / price * 100
                target_pct = (target - price) / price * 100
    except Exception:
        pass

    # 7단계: 종합 + 게이트
    tech_raw = round(
        trend_s     * 0.20 +
        rs_s        * 0.14 +
        structure_s * 0.12 +
        rsi_s       * 0.16 +
        macd_s      * 0.16 +
        bb_s        * 0.08 +
        vol_s       * 0.14
    )
    reversal_confirmed = bull_div and up_bar and vol_ratio >= 1.2 and macd_val > sig_val
    gated = is_down and not reversal_confirmed
    tech_score = min(tech_raw, 45) if gated else tech_raw
    if weekly_up is False and not reversal_confirmed:
        tech_score = min(tech_score, 52)
    if rr is not None and rr < 1.0:
        tech_score = min(tech_score, 50)
    elif rr is not None and rr >= 2.0 and not gated:
        tech_score = min(100, tech_score + 4)
    ext = ((price / ma20) - 1) if ma20 else 0.0
    if ext > 0.15 and not gated:
        tech_score -= 8
    tech_score = max(0, min(100, tech_score))

    # 근거 문장 (차트를 몰라도 읽히게)
    reasons = []
    if with_reasons:
        reasons.append({
            'strong_up':   '강한 상승추세 — 장기선 위, 정배열·우상향',
            'up':          '상승추세 — 장기선 위, 60일선 우상향',
            'range':       '횡보 — 뚜렷한 방향성 없음, 박스권 대응',
            'down':        '하락추세 — 장기선 아래, 60일선 하락',
            'strong_down': '강한 하락추세 — 역배열, 매수 신중',
        }[regime])
        if is_up:
            if rsi < 55:    reasons.append(f'상승추세 속 눌림목(RSI {rsi:.0f}) — 매수 유리 구간')
            elif rsi >= 70: reasons.append(f'과매수(RSI {rsi:.0f}) — 추격 자제, 눌림 대기')
        elif is_down:
            reasons.append('하락추세라 과매도·밴드 하단은 함정 가능 → 매수신호 억제')
        else:
            if rsi < 30:    reasons.append(f'횡보 속 과매도(RSI {rsi:.0f}) — 평균회귀 매수 후보')
            elif rsi > 70:  reasons.append(f'횡보 속 과매수(RSI {rsi:.0f}) — 비중 확대 부적절')
        if macd_cross:
            reasons.append('MACD 골든크로스 발생' + (' (제로라인 위, 강한 신호)' if macd_val > 0 else ' (바닥권 반등 조짐)'))
        elif macd_val > sig_val and hist_rising:
            reasons.append('MACD 상승 모멘텀 강화 중')
        elif macd_val < sig_val and not hist_rising:
            reasons.append('MACD 하락 모멘텀 — 진입 보류')
        if bull_div:
            reasons.append('강세 다이버전스 — 하락 동력 약화, 반전 가능성')
        if up_bar and vol_ratio >= 1.8:
            reasons.append(f'거래량 급증({vol_ratio:.1f}배) 동반 상승 — 매수세 확인')
        elif (not up_bar) and vol_ratio >= 1.8:
            reasons.append(f'거래량 급증({vol_ratio:.1f}배) 동반 하락 — 매도압력')
        elif up_bar and vol_ratio < 0.8:
            reasons.append('상승하지만 거래량 부족 — 신뢰도 낮음')
        if rs_60 is not None:
            if rs_60 > 0.03:    reasons.append(f'시장 대비 강세(상대수익 +{rs_60*100:.0f}%p) — 주도주 성향')
            elif rs_60 < -0.05: reasons.append(f'시장 대비 약세({rs_60*100:.0f}%p) — 지수보다 부진')
        if structure_label == '상승구조(HH·HL)':
            reasons.append('고점·저점 동반 상승(HH·HL) — 추세 구조 건강')
        elif structure_label == '하락구조(LH·LL)':
            reasons.append('고점·저점 동반 하락(LH·LL) — 반등은 되돌림 가능')
        if weekly_up is True:
            reasons.append('주봉도 상승 추세 — 상위 시간프레임 일치')
        elif weekly_up is False:
            reasons.append('주봉 하락/횡보 — 큰 흐름 역행 주의')
        if rr is not None:
            if rr >= 2.0:  reasons.append(f'손익비 양호({rr:.1f}:1) — 손절 -{stop_pct:.0f}% / 목표 +{target_pct:.0f}%')
            elif rr < 1.0: reasons.append(f'손익비 불리({rr:.1f}:1) — 진입 위치 부적절')
            else:          reasons.append(f'손익비 {rr:.1f}:1 — 손절 -{stop_pct:.0f}% / 목표 +{target_pct:.0f}%')
        if ext > 0.15:
            reasons.append(f'20일선 대비 +{ext*100:.0f}% 과열 — 추격 주의')
        if gated:
            reasons.append('※ 하락추세 미확인 반전 → 매수등급 제한(관망 이하)')

    return {
        'tech_score': tech_score,
        'regime': regime,
        'regime_label': regime_label,
        'reasons': reasons,
        'indicators': {
            'rsi': round(rsi, 1),
            'ma20':  round(ma20, 2)  if ma20  else None,
            'ma60':  round(ma60, 2)  if ma60  else None,
            'ma120': round(ma120, 2) if ma120 else None,
            'ma200': round(ma200, 2) if ma200 else None,
            'price': round(price, 2),
            'macd': round(macd_val, 4),
            'macd_signal': round(sig_val, 4),
            'macd_cross': macd_cross,
            'bb_upper': round(bb_upper, 2),
            'bb_mid':   round(bb_mid, 2),
            'bb_lower': round(bb_lower, 2),
            'bb_pct': round(bb_pct * 100, 1),
            'vol_ratio': round(vol_ratio, 2),
            'bull_div': bull_div,
            'ext': round(ext * 100, 1),
            'rs_60': round(rs_60 * 100, 1) if rs_60 is not None else None,
            'structure': structure_label,
            'weekly': weekly_label,
        },
        'plan': {
            'entry':      round(price, 2),
            'stop':       round(stop, 2)       if stop       is not None else None,
            'target':     round(target, 2)     if target     is not None else None,
            'rr':         round(rr, 2)         if rr         is not None else None,
            'stop_pct':   round(stop_pct, 1)   if stop_pct   is not None else None,
            'target_pct': round(target_pct, 1) if target_pct is not None else None,
            'support':    round(support, 2)    if support    is not None else None,
            'resistance': round(resistance, 2) if resistance is not None else None,
            'atr':        round(atr, 2)        if atr        is not None else None,
        },
    }

def _fundamental_signal(info, per, pbr):
    """5대 축 펀더멘털 점수 (전문가형). yfinance info + 계산된 PER/PBR 사용.
    밸류에이션(PEG·다중지표) / 수익성·질 / 성장성 / 재무건전성 / 현금흐름.
    각 축은 가용 지표 평균, 누락 축은 가중치 재정규화로 처리. 네트워크 불필요."""
    g = lambda k: safe_val(info.get(k))
    roe = g('returnOnEquity'); roa = g('returnOnAssets')
    op_m = g('operatingMargins'); net_m = g('profitMargins')
    rev_g = g('revenueGrowth'); earn_g = g('earningsGrowth')
    debt = g('debtToEquity'); cur = g('currentRatio')
    cash = g('totalCash'); tdebt = g('totalDebt')
    fcf = g('freeCashflow'); rev = g('totalRevenue'); mcap = g('marketCap')
    fpe = g('forwardPE'); psr = g('priceToSalesTrailing12Months'); evb = g('enterpriseToEbitda')

    def avg(vals):
        xs = [v for v in vals if v is not None]
        return (sum(xs) / len(xs)) if xs else None

    # ── 1) 밸류에이션 (성장 보정 PEG + 다중 지표 교차) ──
    def sc_per(p):
        if p is None or p <= 0: return None         # 적자는 이익기반 지표 제외
        gg = earn_g * 100 if earn_g is not None else None
        if gg and gg > 0:                            # 성장률 있으면 PEG로 (성장주 보정)
            peg = p / gg
            return 95 if peg < 0.75 else 85 if peg < 1.0 else 70 if peg < 1.5 else 50 if peg < 2.0 else 32 if peg < 3.0 else 18
        return 85 if p < 10 else 72 if p < 15 else 58 if p < 20 else 42 if p < 30 else 28 if p < 50 else 15
    def sc_pbr(pb):
        if pb is None or pb <= 0: return None
        r = roe * 100 if roe is not None else None
        if r is not None and r > 0:                  # ROE 높으면 높은 PBR 정당화 (정당PBR≈ROE/요구수익률)
            ratio = pb / r
            return 92 if ratio < 0.06 else 75 if ratio < 0.10 else 58 if ratio < 0.14 else 42 if ratio < 0.20 else 25
        if r is not None and r <= 0: return 22
        return 88 if pb < 1 else 70 if pb < 2 else 52 if pb < 3 else 35 if pb < 5 else 20
    def sc_psr(s):
        if s is None or s <= 0: return None
        return 88 if s < 1 else 72 if s < 2 else 55 if s < 4 else 35 if s < 8 else 20
    def sc_evb(x):
        if x is None or x <= 0: return None
        return 90 if x < 6 else 72 if x < 10 else 55 if x < 14 else 38 if x < 20 else 20
    def sc_fcfy(f, m):
        if not f or not m or m <= 0: return None
        y = f / m * 100
        return 92 if y > 8 else 80 if y > 5 else 65 if y > 3 else 50 if y > 0 else 25
    valuation = avg([sc_per(per), sc_pbr(pbr), sc_psr(psr), sc_evb(evb), sc_fcfy(fcf, mcap)])

    # ── 2) 수익성·질 (부채로 부풀린 ROE 보정) ──
    def sc_roe(r):
        if r is None: return None
        p = r * 100
        base = 95 if p > 25 else 80 if p > 15 else 65 if p > 10 else 45 if p > 5 else 22 if p > 0 else 10
        if debt is not None and debt > 200 and base > 50: base -= 15
        return base
    def sc_roa(r):
        if r is None: return None
        p = r * 100
        return 92 if p > 12 else 75 if p > 7 else 58 if p > 3 else 40 if p > 0 else 18
    def sc_m(m):
        if m is None: return None
        p = m * 100
        return 90 if p > 20 else 75 if p > 12 else 58 if p > 6 else 40 if p > 0 else 18
    profitability = avg([sc_roe(roe), sc_roa(roa), sc_m(op_m), sc_m(net_m)])

    # ── 3) 성장성 ──
    def sc_g(x):
        if x is None: return None
        p = x * 100
        return 95 if p > 30 else 85 if p > 20 else 72 if p > 12 else 58 if p > 6 else 45 if p > 0 else 28 if p > -10 else 15
    growth = avg([sc_g(rev_g), sc_g(earn_g)])

    # ── 4) 재무 건전성 ──
    def sc_debt(d):
        if d is None: return None
        r = d / 100
        return 92 if r < 0.3 else 78 if r < 0.6 else 60 if r < 1.0 else 42 if r < 2.0 else 22
    def sc_cur(c):
        if c is None: return None
        return 90 if c > 2 else 75 if c > 1.5 else 58 if c > 1 else 35
    def sc_nc(c, d):
        if c is None or d is None: return None
        if d == 0: return 95
        return 90 if (c - d) > 0 else 60 if c / d > 0.5 else 40 if c / d > 0.2 else 25
    health = avg([sc_debt(debt), sc_cur(cur), sc_nc(cash, tdebt)])

    # ── 5) 현금흐름 (FCF 양수·마진) ──
    def sc_fcf(f, r):
        if f is None: return None
        if f <= 0: return 22
        if r and r > 0:
            m = f / r * 100
            return 92 if m > 15 else 78 if m > 8 else 62 if m > 3 else 48
        return 60
    cashflow = avg([sc_fcf(fcf, rev)])

    pillars = {'valuation': valuation, 'profitability': profitability,
               'growth': growth, 'health': health, 'cashflow': cashflow}
    weights = {'valuation': 0.28, 'profitability': 0.24, 'growth': 0.20, 'health': 0.16, 'cashflow': 0.12}
    avail = {k: v for k, v in pillars.items() if v is not None}
    if avail:
        wsum = sum(weights[k] for k in avail)
        fund_score = round(sum(avail[k] * weights[k] for k in avail) / wsum)
    else:
        fund_score = 50

    # ── 근거 문장 ──
    peg_val = (per / (earn_g * 100)) if (per and per > 0 and earn_g and earn_g > 0) else None
    reasons = []
    if valuation is not None and valuation >= 68:
        reasons.append('밸류에이션 매력적' + (f' — PEG {peg_val:.1f} (성장 대비 저평가)' if (peg_val and peg_val < 1) else ''))
    elif valuation is not None and valuation < 40:
        reasons.append('밸류에이션 부담' + (f' — PEG {peg_val:.1f} (성장 대비 고평가)' if (peg_val and peg_val > 2) else ''))
    if per is None or per <= 0:
        reasons.append('적자 — 이익기반 지표 제외, 매출·현금흐름 위주 평가')
    if roe is not None:
        rp = roe * 100
        if rp > 15:  reasons.append(f'높은 자본수익성(ROE {rp:.0f}%)' + (' — 단, 부채 과다로 질 낮음' if (debt and debt > 200) else ''))
        elif rp < 5: reasons.append(f'낮은 자본수익성(ROE {rp:.0f}%)')
    if growth is not None:
        if growth >= 70:  reasons.append('성장성 우수 — 매출·이익 견조한 성장')
        elif growth < 40: reasons.append('성장 정체 또는 역성장')
    if health is not None:
        if health >= 70:  reasons.append('재무 건전 — 낮은 부채·충분한 유동성')
        elif health < 40: reasons.append('재무 부담 — 높은 부채 또는 유동성 취약')
    if cashflow is not None:
        if cashflow >= 62: reasons.append('견조한 잉여현금흐름(FCF)')
        elif cashflow <= 22: reasons.append('잉여현금흐름 적자 — 현금 창출력 약함')

    return {
        'fund_score': fund_score,
        'pillars': {k: (round(v) if v is not None else None) for k, v in pillars.items()},
        'reasons': reasons,
        'metrics': {
            'per': per, 'forwardPer': fpe, 'pbr': pbr, 'psr': psr, 'evEbitda': evb,
            'roe': roe, 'roa': roa, 'operatingMargin': op_m, 'netMargin': net_m,
            'revenueGrowth': rev_g, 'earningsGrowth': earn_g,
            'peg': (round(peg_val, 2) if peg_val else None),
            'debtToEquity': debt, 'currentRatio': cur, 'freeCashflow': fcf,
            'fcfMargin': (round(fcf / rev * 100, 1) if (fcf and rev and rev > 0) else None),
        },
    }

def _signal_base(ticker):
    """신호 계산 중 '장중 불변·고비용' 부분만 캐싱한다.
    (야후 2년 일봉 배열·주봉추세·info·야후 PER/PBR — 모두 장중에 바뀌지 않거나
    분기 단위로만 바뀜) 실시간 가격·네이버 오버레이·점수 계산은 캐싱하지 않고
    매 요청마다 새로 한다. 데이터 부족 시 None."""
    cached = _cache_get(('sigbase', ticker), 1800)
    if cached is not None:
        return cached
    t = yf.Ticker(ticker)
    # 200일선·기울기 판정을 위해 2년치 일봉 확보
    hist = t.history(period='2y', interval='1d')
    if hist.empty or len(hist) < 60:
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
    info = t.info
    try:
        fi = t.fast_info
        if fi.last_price:
            info['currentPrice'] = fi.last_price
    except Exception:
        pass
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
    try:
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
            except Exception:
                pass

        # 시장 대비 상대강도 (지수 종가는 1시간 캐시)
        rs_60 = None
        try:
            bclos = _benchmark_closes(_benchmark_symbol(ticker))
            if len(bclos) > 61 and n > 61:
                rs_60 = (closes[-1] / closes[-61] - 1) - (bclos[-1] / bclos[-61] - 1)
        except Exception:
            pass

        # 기술적 매수 점수 (백테스트와 동일한 순수 엔진을 공유)
        ts = _technical_signal(closes, highs, lows, vols, rs_60, weekly_up)
        tech_score = ts['tech_score']
        # 5대 축 전문가형 펀더멘털 점수
        fs = _fundamental_signal(info, per, pbr)
        fund_score = fs['fund_score']

        combined = round(tech_score * 0.55 + fund_score * 0.45)

        def lab(s):
            if s >= 68: return '매수 고려'
            if s >= 48: return '관망'
            return '주의'
        label = lab(combined)

        data = {
            'combined': combined,
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
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backtest/<path:ticker>')
def backtest(ticker):
    """실시간 신호와 '동일한' 기술 엔진(_technical_signal)을 과거 전 구간에 적용해
    매수 규칙의 성과를 검증한다. 점수 >= 68(매수 고려)에서 진입, ATR 매매플랜의
    손절/목표 또는 최대 보유기간 도달 시 청산. 상대강도·주봉은 백테스트에서 제외(중립)."""
    cached = _cache_get(('backtest', ticker), 3600)
    if cached:
        return jsonify({'success': True, 'data': cached})
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period='2y', interval='1d')
        if hist.empty or len(hist) < 120:
            return jsonify({'success': False, 'error': '데이터 부족 (최소 120거래일 필요)'}), 422

        closes = [float(v) for v in hist['Close'].tolist()]
        highs  = [float(v) for v in hist['High'].tolist()]
        lows   = [float(v) for v in hist['Low'].tolist()]
        vols   = [float(v) for v in hist['Volume'].tolist()]
        n = len(closes)

        BUY_TH, HOLD_MAX, START = 68, 20, 70   # 매수기준 / 최대보유(거래일) / 시작 인덱스
        trades = []
        i = START
        while i < n - 1:
            sub = _technical_signal(closes[:i + 1], highs[:i + 1], lows[:i + 1],
                                    vols[:i + 1], rs_60=None, weekly_up=None, with_reasons=False)
            if sub['tech_score'] >= BUY_TH:
                entry = closes[i]
                plan = sub['plan']
                stop, target = plan['stop'], plan['target']
                exit_price, exit_reason, bars = None, None, 0
                for j in range(i + 1, min(i + 1 + HOLD_MAX, n)):
                    bars = j - i
                    if stop is not None and lows[j] <= stop:        # 손절 우선(보수적)
                        exit_price, exit_reason = stop, '손절'; break
                    if target is not None and highs[j] >= target:
                        exit_price, exit_reason = target, '목표'; break
                if exit_price is None:
                    exit_price, exit_reason = closes[min(i + HOLD_MAX, n - 1)], '기간만료'
                ret = (exit_price / entry - 1) * 100 if entry else 0
                trades.append({'ret': ret, 'reason': exit_reason, 'bars': bars or 1})
                i += (bars or 1)        # 보유기간 동안 중복 진입 방지
            else:
                i += 1

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
        bh = (closes[-1] / closes[START] - 1) * 100   # 동일기간 단순보유 수익률

        data = {
            'trades': total,
            'win_rate': round(len(wins) / total * 100, 1) if total else None,
            'avg_return': round(sum(rets) / total, 2) if total else None,
            'avg_win': round(sum(wins) / len(wins), 2) if wins else None,
            'avg_loss': round(sum(losses) / len(losses), 2) if losses else None,
            'expectancy': round(sum(rets) / total, 2) if total else None,   # 1회 기대수익(%)
            'profit_factor': round(gp / gl, 2) if gl > 0 else None,
            'strategy_return': round((eq - 1) * 100, 1),                    # 전략 누적(복리)
            'buy_hold_return': round(bh, 1),
            'max_drawdown': round(mdd * 100, 1),
            'exits': {
                'target': sum(1 for tr in trades if tr['reason'] == '목표'),
                'stop':   sum(1 for tr in trades if tr['reason'] == '손절'),
                'time':   sum(1 for tr in trades if tr['reason'] == '기간만료'),
            },
            'params': {'buy_threshold': BUY_TH, 'max_hold_days': HOLD_MAX, 'period': '2y'},
            'note': '수수료·슬리피지 미반영. 상대강도·주봉 필터는 백테스트에서 제외(중립). 과거 성과가 미래를 보장하지 않음.',
        }
        _cache_set(('backtest', ticker), data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
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


# 프론트엔드(index.html) 서빙 — API와 같은 서버에서 제공
@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(__file__), 'index.html'))


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('PORT', '5001'))
    app.run(debug=debug, host='0.0.0.0', port=port)
