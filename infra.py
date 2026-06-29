"""MarketEye 공용 인프라 — 로깅·타이밍·TTL 캐시·JSON NaN/Inf 방어막.

Flask 앱이나 데이터 수집 코드에 의존하지 않는 자기완결 모듈.
app.py·providers.py 등 모든 모듈이 이 인프라를 공유한다(순환 import 방지).
분리 전에는 app.py 안에 있었고 로직은 그대로 옮겨졌다(동작 동일).
"""
import os
import time
import math
import random
import threading
import contextlib
from itertools import count
from flask.json.provider import DefaultJSONProvider


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

def _cache_get_stale(key):
    """TTL을 무시하고 캐시에 남아 있는 값을 반환 (없으면 None).
    야후 레이트리밋으로 신규 조회가 실패했을 때, 만료된 값이라도 빈 화면·달러
    표기 대신 보여주기 위한 graceful degradation 폴백용."""
    with _cache_lock:
        e = _cache.get(key)
        return e[1] if e else None


# ── 야후(yfinance) 레이트리밋 완화 ────────────────────────────────────
# 동시 다발 호출이 'Too Many Requests'(429)를 유발해 종목 조회가 통째로 500나던
# 문제를 완화한다: ① 전역 최소 간격으로 호출 폭주를 막고 ② 429면 백오프 재시도.
def is_rate_limited(exc):
    """야후/yfinance 레이트리밋(429) 예외인지 판별."""
    s = str(exc).lower()
    return 'too many requests' in s or 'rate limit' in s or '429' in s

# 전역 최소 호출 간격(초). 0이면 비활성. 동시 요청이 야후를 때리는 빈도를 낮춘다.
YF_MIN_INTERVAL = float(os.environ.get('YF_MIN_INTERVAL', '0.35'))
YF_RETRIES = int(os.environ.get('YF_RETRIES', '2'))
_yf_lock = threading.Lock()
_yf_last = [0.0]

def yf_call(fn, label='yf', retries=None):
    """yfinance 네트워크 호출(fn: 인자 없는 callable)을 전역 간격 제한 + 레이트리밋
    백오프 재시도로 감싼다. 호출부는 yf_call(lambda: t.info, 'yf.info AAPL') 형태.
    레이트리밋이 아닌 예외는 즉시 전파한다."""
    retries = YF_RETRIES if retries is None else retries
    last_exc = None
    for attempt in range(retries + 1):
        if YF_MIN_INTERVAL > 0:
            # 진입 시점만 전역적으로 띄운다(실제 네트워크 대기까지 직렬화하진 않음).
            with _yf_lock:
                wait = YF_MIN_INTERVAL - (time.time() - _yf_last[0])
                if wait > 0:
                    time.sleep(wait)
                _yf_last[0] = time.time()
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if is_rate_limited(e) and attempt < retries:
                back = (2 ** attempt) * 0.5 + random.random() * 0.4
                log(f'{label} 레이트리밋 — {back:.1f}s 후 재시도 ({attempt + 1}/{retries})', 'WARN')
                time.sleep(back)
                continue
            raise
    raise last_exc

