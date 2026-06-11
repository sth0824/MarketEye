from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import requests
import traceback
import json
import io
import unicodedata

app = Flask(__name__)
CORS(app)

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

# 앱 시작 시 비동기로 KRX 로드
import threading
threading.Thread(target=_load_krx, daemon=True).start()


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


@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    try:
        t = yf.Ticker(ticker.upper())
        info = t.info

        # 현재가
        price = safe_val(info.get('currentPrice') or info.get('regularMarketPrice'))

        # 52주 범위
        low52 = safe_val(info.get('fiftyTwoWeekLow'))
        high52 = safe_val(info.get('fiftyTwoWeekHigh'))

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
            'per': safe_val(info.get('trailingPE')),
            'forwardPer': safe_val(info.get('forwardPE')),
            'pbr': safe_val(info.get('priceToBook')),
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
            'eps': safe_val(info.get('trailingEps')),
            'forwardEps': safe_val(info.get('forwardEps')),
            'bookValue': safe_val(info.get('bookValue')),

            # 배당
            'dividendYield': safe_val(info.get('dividendYield')),
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
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/batch')
def get_batch():
    tickers = request.args.get('tickers', '')
    ticker_list = [t.strip() for t in tickers.split(',') if t.strip()]
    results = {}
    for ticker in ticker_list:
        try:
            resp = get_stock(ticker)
            results[ticker.upper()] = resp.get_json()
        except Exception as e:
            results[ticker.upper()] = {'success': False, 'error': str(e)}
    return jsonify(results)


if __name__ == '__main__':
    app.run(debug=True, port=5001)
