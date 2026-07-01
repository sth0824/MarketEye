"""MarketEye 외부 데이터 수집층 — KRX 종목·네이버 실시간 시세·야후 종목 데이터.

KRX 종목 목록(검색·한글명), 네이버 실시간 시세/펀더멘털, 야후 종목 전체 데이터를
조회한다. infra(로깅·타이밍)와 signals(safe_val)에만 의존하며 app(라우트)에는
의존하지 않는다(순환 import 방지). 모듈 import 시 KRX 번들을 동기 로드하고
라이브 갱신 스레드를 띄운다 — 분리 전 app.py 로드 시점 동작과 동일.
로직은 그대로 옮겨졌다(동작 동일).
"""
import os
import json
import re
import unicodedata
import threading
import concurrent.futures
import requests
import yfinance as yf

from infra import log, timed, set_tag, _tag, _cache_get, _cache_set, _cache_get_stale, yf_call, is_rate_limited
from signals import safe_val

# 네이버 실시간 시세 캐시 TTL(초). 같은 종목 연속 조회 시 재호출을 막되,
# 짧게 잡아 실시간성과 호출 절감을 절충한다 (환경변수로 조정 가능).
NAVER_TTL = int(os.environ.get('NAVER_TTL', '12'))


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
            log('KRX 라이브: 파싱된 행 없음', 'WARN')
            return

        header = rows[0]
        code_idx = next((i for i,h in enumerate(header) if '종목코드' in h), None)
        name_idx = next((i for i,h in enumerate(header) if '회사명' in h), None)
        mkt_idx  = next((i for i,h in enumerate(header) if '시장구분' in h), None)

        if code_idx is None or name_idx is None:
            log(f'KRX 라이브: 헤더 파싱 실패 {header}', 'WARN')
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
            log('KRX 라이브: 유효 종목 0건 — 기존 데이터 유지', 'WARN')
            return
        _krx_stocks = results
        log(f'KRX 라이브 갱신 완료: {len(_krx_stocks)}종목')
    except Exception as e:
        log(f'KRX 라이브 로드 실패: {e}', 'WARN')

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
        log(f'KRX 번들 로드 완료: {len(_krx_stocks)}종목')
        return True
    except Exception as e:
        log(f'KRX 번들 로드 실패: {e}', 'ERROR')
        return False

# 앱 시작 시 KRX 로드.
# 번들은 '동기'로 즉시 로드 → 첫 요청부터 한글 종목명 보장(콜드스타트 레이스로
# 영어 이름이 캐시에 박히는 문제 방지). 느린 라이브 갱신만 백그라운드로 돌린다.
_load_krx_bundle()               # 로컬 파일 — 빠름. 즉시 _krx_name 사용 가능

def _krx_live_refresh():
    try:
        _load_krx()              # KRX 사이트 접근 가능하면 최신 목록으로 교체
    except Exception:
        pass                     # 실패해도 번들 데이터 유지

threading.Thread(target=_krx_live_refresh, daemon=True).start()

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
    code: 종목코드 6자리(예: '005930'). 실패 시 예외를 던진다.

    basic(실시간 시세)·integration(펀더멘털)은 서로 독립적인 엔드포인트라
    병렬로 조회해 지연을 줄인다(순차 대비 약 절반). 결과는 NAVER_TTL초 동안
    캐시해 같은 종목 연속 조회 시 재호출을 막는다(짧은 TTL이라 실시간성 유지).
    조회 실패 시 예외가 전파되며(캐시에 저장하지 않음) 호출부가 야후값으로 폴백한다."""
    cached = _cache_get(('naver', code), NAVER_TTL)
    if cached is not None:
        return cached

    out = {}
    # basic·integration 두 엔드포인트를 동시에 조회 (순차 → 병렬로 지연 절감)
    parent = _tag()

    def _get(kind):
        set_tag(parent)   # 워커 스레드 로그도 부모 요청 태그로 추적
        with timed(f'네이버 {kind} {code}', warn_ms=1500, slow_ms=3000):
            return requests.get(f'https://m.stock.naver.com/api/stock/{code}/{kind}',
                                headers=_NAVER_HEADERS, timeout=5).json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_basic = ex.submit(_get, 'basic')
        f_integ = ex.submit(_get, 'integration')
        b = f_basic.result()
        i = f_integ.result()

    # 1) 실시간 시세 (현재가·등락) — basic 엔드포인트
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

    # 2) 펀더멘털 + 당일 OHLC·52주 — integration 엔드포인트 (위에서 병렬 조회됨)
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
    _cache_set(('naver', code), out)
    return out


def _yahoo_search(query):
    url = 'https://query1.finance.yahoo.com/v1/finance/search'
    params = {'q': query, 'lang': 'en-US', 'region': 'US',
              'quotesCount': 10, 'newsCount': 0, 'enableFuzzyQuery': True, 'enableCb': False}
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    with timed(f'야후 검색 "{query}"', warn_ms=1500, slow_ms=3000):
        res = requests.get(url, params=params, headers=headers, timeout=5)
    return res.json().get('quotes', [])


def _is_korean(s):
    return any('가' <= c <= '힣' or 'ᄀ' <= c <= 'ᇿ' for c in s)



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

            # 분기 재무제표로 TTM 계산 (yf_call로 레이트리밋 완화)
            qfin = yf_call(lambda: t.quarterly_financials, 'yf.qfin')
            qbs  = yf_call(lambda: t.quarterly_balance_sheet, 'yf.qbs')

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


def _kr_skeleton(ticker):
    """야후 조회가 레이트리밋 등으로 실패했을 때 한국 종목용 빈 골격 dict.
    이후 _fetch_naver 오버레이가 가격·펀더멘털을 채운다. 통화는 KRW로 고정해
    야후 결측 시 'USD' 기본값으로 달러 표기되던 문제를 막는다."""
    return {
        'ticker': ticker.upper(),
        'name': _krx_name(ticker) or ticker.upper(),
        'sector': '-', 'industry': '-', 'currency': 'KRW', 'exchange': '-',
        'history': [],
    }


def _fetch_stock_yahoo(ticker):
    """yfinance에서 종목 데이터를 조회해 dict로 반환 (실패 시 예외).
    레이트리밋이 잦은 야후 호출은 yf_call로 전역 간격 제한·백오프 재시도한다."""
    _t = ticker.upper()
    is_kr = _t.endswith('.KS') or _t.endswith('.KQ')
    t = yf.Ticker(_t)

    # info: 야후 스크레이프 — 보통 stock 응답에서 가장 무거운 단계
    with timed(f'yf.info {_t}'):
        info = yf_call(lambda: t.info, f'yf.info {_t}')
    log(f'yf.info {_t} keys={len(info)}', 'DEBUG')

    # fast_info로 실시간에 가까운 가격 보완 (price 계산 전에 먼저 반영)
    try:
        with timed(f'yf.fast_info {_t}', warn_ms=1500, slow_ms=3000):
            fi = yf_call(lambda: t.fast_info, f'yf.fast_info {_t}')
            if fi.last_price:
                info['currentPrice'] = fi.last_price
            if fi.previous_close:
                info['regularMarketPreviousClose'] = fi.previous_close
    except Exception as e:
        log(f'yf.fast_info {_t} 실패: {e}', 'WARN')

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

    # 배당수익률: 최신 yfinance는 퍼센트 숫자(0.36 = 0.36%)로 반환 → 비율(fraction)로 정규화
    div_yield = safe_val(info.get('dividendYield'))
    if div_yield is not None:
        div_yield = div_yield / 100

    # PER/PBR (yfinance 미제공 시 분기 재무제표를 추가 조회 → 느릴 수 있음)
    with timed(f'PER/PBR 계산 {_t}', warn_ms=2000, slow_ms=4000):
        per, pbr, eps_calc, bps_calc = _calc_per_pbr(info, t)
    log(f'PER/PBR {_t} per={per} pbr={pbr}', 'DEBUG')

    # 최근 1년 종가 히스토리 (차트용 - 월별)
    with timed(f'yf.history(1y) {_t}', warn_ms=2000, slow_ms=4000):
        hist = yf_call(lambda: t.history(period='1y', interval='1mo'), f'yf.history {_t}')
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
            # 한국 종목(.KS/.KQ)은 야후 info 결측 시에도 KRW로 고정 (달러 표기 방지)
            'currency': info.get('currency') or ('KRW' if is_kr else 'USD'),
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
    return data


def _card_fundamentals(info):
    """야후 info에서 '네이버가 주지 않는' 카드 펀더멘털 필드만 뽑아 반환(None 제외).
    야후 조회 실패 시 직전값·신호용 sigbase의 info로 카드를 보강해, ROE·마진·부채·
    매출·Beta 등이 '-'로 사라지는 문제와 '카드엔 없고 신호엔 있는' 불일치를 없앤다.
    이 값들은 분기 재무 기반이라 장중 실시간이 필요 없다."""
    g = lambda k: safe_val(info.get(k))
    out = {
        'roe': g('returnOnEquity'), 'roa': g('returnOnAssets'),
        'grossMargin': g('grossMargins'), 'operatingMargin': g('operatingMargins'),
        'netMargin': g('profitMargins'), 'ebitdaMargin': g('ebitdaMargins'),
        'revenueGrowth': g('revenueGrowth'), 'earningsGrowth': g('earningsGrowth'),
        'earningsQuarterlyGrowth': g('earningsQuarterlyGrowth'),
        'debtToEquity': g('debtToEquity'), 'currentRatio': g('currentRatio'),
        'quickRatio': g('quickRatio'), 'totalCash': g('totalCash'),
        'totalDebt': g('totalDebt'), 'freeCashflow': g('freeCashflow'),
        'revenue': g('totalRevenue'), 'ebitda': g('ebitda'),
        'enterpriseValue': g('enterpriseValue'), 'evRevenue': g('enterpriseToRevenue'),
        'beta': g('beta'), 'sharesOutstanding': g('sharesOutstanding'),
        'floatShares': g('floatShares'), 'shortRatio': g('shortRatio'),
        'payoutRatio': g('payoutRatio'), 'targetPrice': g('targetMeanPrice'),
        'recommendationMean': g('recommendationMean'),
    }
    if info.get('sector'):
        out['sector'] = info.get('sector')
    if info.get('industry'):
        out['industry'] = info.get('industry')
    return {k: v for k, v in out.items() if v is not None}


def _fetch_stock(ticker):
    """종목 데이터를 조립해 반환. 한국 종목은 네이버 실시간으로 보강하며,
    야후가 레이트리밋 등으로 실패해도 네이버 단독으로 응답을 구성한다.

    레이트리밋 동작:
      · 한국 종목: 야후 실패 → 빈 골격 + 네이버 오버레이로 KRW 시세·펀더멘털 제공.
        (야후가 죽어도 가격·PER/PBR·시총이 정상 표기 — 달러·빈값 문제 해소)
      · 해외 종목: 폴백이 없으므로 예외를 그대로 전파(호출부가 stale 캐시로 폴백).
    """
    tk = ticker.upper()
    is_kr = tk.endswith('.KS') or tk.endswith('.KQ')

    try:
        data = _fetch_stock_yahoo(ticker)
    except Exception as e:
        if not is_kr:
            raise
        # 한국 종목: 야후가 죽어도 네이버로 응답을 만든다.
        lvl = 'WARN' if is_rate_limited(e) else 'ERROR'
        log(f'야후 조회 {tk} 실패 — 네이버 단독 폴백: {e}', lvl)
        # 스켈레톤만 쓰면 ROE·마진·부채·매출·Beta 등 야후 info 필드가 '-'로 사라지고
        # 다음 캐시에 덮여 유실된다(카드=신호 불일치). 직전 정상값에서 살려온다:
        #  ① 직전 stock 캐시 → ② 신호용 sigbase의 야후 info(신호가 이미 받아둔 값).
        stale = _cache_get_stale(('stock', tk))
        data = dict(stale) if isinstance(stale, dict) else _kr_skeleton(ticker)
        if data.get('roe') is None:
            sb = _cache_get_stale(('sigbase', tk))
            if isinstance(sb, dict) and isinstance(sb.get('info'), dict):
                data.update(_card_fundamentals(sb['info']))
                log(f'{tk} 펀더멘털을 신호 sigbase에서 보강', 'DEBUG')

    # 한국 종목은 네이버 실시간 시세·펀더멘털로 보강 (야후 KRX 15~20분 지연 해소).
    if is_kr:
        try:
            with timed(f'네이버 보강 {tk}', warn_ms=2000, slow_ms=4000):
                nv = _fetch_naver(tk.split('.')[0])
            for k, v in nv.items():
                if v is not None:
                    data[k] = v
        except Exception as e:
            log(f'네이버 보강 {tk} 실패(야후값 폴백): {e}', 'WARN')
            # 야후도 실패해 골격뿐인데 네이버까지 실패하면 가격이 전혀 없다 → 예외 전파
            if data.get('price') is None:
                raise

    return data

