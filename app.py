from flask import Flask, jsonify, request, send_file
from flask.json.provider import DefaultJSONProvider
from flask_cors import CORS
import yfinance as yf
import requests
import traceback
import json
import math
import os
import time
import threading
import unicodedata
import re
import contextlib
from itertools import count
# 순수 신호 엔진은 signals.py로 분리 (동작 동일). app.py는 라우트·수집·조립 담당.
from signals import (
    safe_val,
    _technical_signal,
    _fundamental_signal,
    _composite_signal,
)

app = Flask(__name__)
CORS(app)


# ── JSON NaN/Inf 전역 방어막 ────────────────────────────────────────────
# Python 기본 json은 NaN/Infinity를 그대로 출력하지만 이는 표준 JSON이 아니라
# 브라우저 JSON.parse(=res.json())가 거부한다. 과거 signal 응답의 rs_60 등이
# NaN으로 새어 나가 진입 신호 탭 전체가 '분석 중'에서 멈췄다. 모든 응답에서
# 비유한(NaN/Inf) float를 재귀적으로 null로 치환해 근본적으로 차단한다.
def _json_safe(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


class SafeJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        return super().dumps(_json_safe(obj), **kwargs)


app.json = SafeJSONProvider(app)

# ══════════════════════════════════════════════════════════════════════
#  로깅 유틸 — 유지보수용 통합 로그
#
#  설계 의도:
#   1) 요청 태그: 동시 요청 10개가 서버 로그에 뒤섞여도 '#7 010120.KS'
#      같은 태그로 어느 요청의 줄인지 한눈에 구분된다(스레드 로컬).
#   2) timed(): with 블록의 소요시간을 자동 측정·임계 초과 시 WARN 승격.
#      네트워크/연산 경계마다 같은 포맷으로 찍혀 병목을 바로 식별한다.
#   3) flush=True: Render/gunicorn은 stdout을 버퍼링해 로그가 늦거나
#      순서가 꼬인다 → 매 줄 flush로 실시간·정순 보장.
#   4) LOG_LEVEL 환경변수(DEBUG/INFO/WARN/ERROR)로 상세도 조절.
# ══════════════════════════════════════════════════════════════════════
_LEVELS = {'DEBUG': 10, 'INFO': 20, 'WARN': 30, 'ERROR': 40}
_MIN_LEVEL = _LEVELS.get(os.environ.get('LOG_LEVEL', 'INFO').upper(), 20)
_req_seq = count(1)
_local = threading.local()

def _tag():
    return getattr(_local, 'tag', '-')

def set_tag(tag):
    """현재 스레드의 로그 태그 지정 (batch 워커 스레드 등에서 직접 호출)."""
    _local.tag = tag

def log(msg, level='INFO'):
    if _LEVELS.get(level, 20) < _MIN_LEVEL:
        return
    ts = time.strftime('%H:%M:%S')
    print(f'{ts} [{level:<5}] [{_tag()}] {msg}', flush=True)

@contextlib.contextmanager
def timed(label, warn_ms=3000, slow_ms=8000):
    """with 블록 소요시간을 측정해 로깅. 느리면 자동으로 마크·WARN 승격."""
    t0 = time.time()
    log(f'▶ {label}', 'DEBUG')
    try:
        yield
    finally:
        ms = int((time.time() - t0) * 1000)
        mark = ' 🐢 매우느림' if ms > slow_ms else (' ⏱ 느림' if ms > warn_ms else '')
        log(f'✓ {label} {ms}ms{mark}', 'WARN' if ms > warn_ms else 'INFO')

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
    code: 종목코드 6자리(예: '005930'). 실패 시 예외를 던진다."""
    out = {}

    # 1) 실시간 시세 (현재가·등락) — basic 엔드포인트
    with timed(f'네이버 basic {code}', warn_ms=1500, slow_ms=3000):
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
    with timed(f'네이버 integration {code}', warn_ms=1500, slow_ms=3000):
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
    with timed(f'야후 검색 "{query}"', warn_ms=1500, slow_ms=3000):
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
    _t = ticker.upper()
    t = yf.Ticker(_t)

    # info: 야후 스크레이프 — 보통 stock 응답에서 가장 무거운 단계
    with timed(f'yf.info {_t}'):
        info = t.info
    log(f'yf.info {_t} keys={len(info)}', 'DEBUG')

    # fast_info로 실시간에 가까운 가격 보완 (price 계산 전에 먼저 반영)
    try:
        with timed(f'yf.fast_info {_t}', warn_ms=1500, slow_ms=3000):
            fi = t.fast_info
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
            with timed(f'네이버 보강 {tk}', warn_ms=2000, slow_ms=4000):
                nv = _fetch_naver(tk.split('.')[0])
            for k, v in nv.items():
                if v is not None:
                    data[k] = v
        except Exception as e:
            log(f'네이버 보강 {tk} 실패(야후값 폴백): {e}', 'WARN')

    return data


# 전체 데이터 TTL (초). 환경변수로 조정 가능.
STOCK_TTL = int(os.environ.get('STOCK_TTL', '60'))
PRICE_TTL = int(os.environ.get('PRICE_TTL', '15'))


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
        log(f'stock {key} 실패: {e}', 'ERROR')
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


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
        log(f'price {key} 실패: {e}', 'ERROR')
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


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
            h = yf.Ticker(symbol).history(period='1y', interval='1d')
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
    with timed(f'yf.history(2y) {ticker}'):
        hist = t.history(period='2y', interval='1d')
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
        info = t.info
    try:
        fi = t.fast_info
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

        # 점수 계산은 순수 연산 — 통째로 한 번만 측정(보통 수 ms, 느리면 데이터 이상)
        with timed(f'signal 점수계산 {ticker}', warn_ms=500, slow_ms=1500):
            # 기술적 매수 점수 (백테스트와 동일한 순수 엔진을 공유)
            ts = _technical_signal(closes, highs, lows, vols, rs_60, weekly_up)
            tech_score = ts['tech_score']
            # 5대 축 전문가형 펀더멘털 점수
            fs = _fundamental_signal(info, per, pbr)
            fund_score = fs['fund_score']

        # 가치 점수 신뢰도: 5대 축 중 데이터가 있는 축의 가중 비율 (0~1)
        _pw = {'valuation': 0.28, 'profitability': 0.24, 'growth': 0.20, 'health': 0.16, 'cashflow': 0.12}
        fund_conf = sum(_pw[k] for k, v in fs['pillars'].items() if v is not None)
        # 전문가식 종합 점수 (차트·가치를 관계·신뢰도까지 고려해 합성)
        comp = _composite_signal(tech_score, fund_score, ts['regime'], ts['plan'].get('rr'), fund_conf)
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
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        log(f'signal {ticker} 실패: {e}', 'ERROR')
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


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
        with timed(f'yf.history(2y,backtest) {ticker}'):
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
        # 과거 전 구간에 신호 엔진을 반복 적용 — backtest의 핵심 CPU 비용 (O(n²))
        _bt0 = time.time()
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
        log(f'backtest 매매시뮬 {ticker} {int((time.time()-_bt0)*1000)}ms ({len(trades)}건)',
            'WARN' if (time.time() - _bt0) > 1.5 else 'INFO')

        # ── 점수 구간별 미래수익 검증 (점수의 단조성: 높은 점수 = 높은 미래수익?) ──
        # 매 거래일의 기술점수와 그 시점 이후 HOLD_MAX일 단순 수익률을 짝지어 구간 통계.
        _bk0 = time.time()
        BUCKETS = [(0, 40), (40, 55), (55, 68), (68, 80), (80, 101)]
        bkt = {f'{lo}-{hi if hi <= 100 else 100}': [] for lo, hi in BUCKETS}
        for k in range(START, n - 1):
            s = _technical_signal(closes[:k + 1], highs[:k + 1], lows[:k + 1],
                                  vols[:k + 1], rs_60=None, weekly_up=None, with_reasons=False)['tech_score']
            fwd = (closes[min(k + HOLD_MAX, n - 1)] / closes[k] - 1) * 100 if closes[k] else 0
            for lo, hi in BUCKETS:
                if lo <= s < hi:
                    bkt[f'{lo}-{hi if hi <= 100 else 100}'].append(fwd)
                    break
        score_buckets = [
            {'range': key,
             'n': len(v),
             'avg_fwd': round(sum(v) / len(v), 2) if v else None,
             'win_rate': round(sum(1 for x in v if x > 0) / len(v) * 100, 1) if v else None}
            for key, v in bkt.items()
        ]
        log(f'backtest 구간검증 {ticker} {int((time.time()-_bk0)*1000)}ms', 'DEBUG')
        # 단조성 점검: 인접 구간 평균수익이 우상향하는 비율 (1.0이면 완전 단조)
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
            'score_buckets': score_buckets,   # 점수 구간별 미래 20일 수익·승률 (단조성 검증용)
            'monotonic': monotonic,           # 인접 구간 우상향 비율 (1.0=완전 단조)
            'note': '수수료·슬리피지 미반영. 상대강도·주봉 필터는 백테스트에서 제외(중립). 과거 성과가 미래를 보장하지 않음. 점수 구간 검증은 차트(기술) 점수 한정 — 가치 점수는 과거 시점 데이터 제약으로 검증 불가.',
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


# 프론트엔드(index.html) 서빙 — API와 같은 서버에서 제공
@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(__file__), 'index.html'))


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('PORT', '5001'))
    app.run(debug=debug, host='0.0.0.0', port=port)
