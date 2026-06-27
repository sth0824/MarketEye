"""MarketEye 순수 신호 엔진.

가격/거래량 배열과 펀더멘털 dict만으로 매수 점수를 계산하는 순수 함수 모음.
네트워크·캐시·Flask에 의존하지 않으므로 단독 import·테스트가 가능하다.
app.py(라우트)와 backtest가 이 엔진을 공유한다.

분리 전에는 app.py 안에 있었고 로직은 그대로 옮겨졌다(동작 동일).
"""

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

def _composite_signal(tech_score, fund_score, regime, rr, fund_conf=1.0):
    """차트(기술)·가치(펀더멘털)를 전문가식으로 합성한 단일 '진입 점수'.

    설계 원칙(이중 계산·과신 방지):
      1) 가중 기하평균(차트0.55:가치0.45) → 한쪽이 부실하면 상쇄 불가(균형 요구)
      2) 데이터 신뢰도 수축 — 가치 지표가 듬성하면 가치점수를 중립(50)으로 당김
      3) 관계 조정 — 양쪽 동의 가산·크게 엇갈리면 신뢰 저하 감산
      4) 안전 레일 — '강한 하락추세'만 하드캡(떨어지는 칼날 회피)
    ※ 손익비·추세는 이미 차트 점수(tech_score) 안에 반영돼 있으므로 여기서
      다시 깎지 않는다(이중 계산 제거). 손익비는 차트 점수를 통해 한 번만 반영됨.
    반환: 0~100 점수 + 신뢰도 + '사도 될지 말지' 한 줄 판정/근거."""
    t = max(1, tech_score)
    # 2) 데이터 신뢰도 수축: 가용 가치지표가 적을수록 가치점수를 50으로 끌어당김
    fund_conf = max(0.0, min(1.0, fund_conf))
    f_adj = max(1.0, 50 + (fund_score - 50) * fund_conf)

    # 1) 가중 기하평균
    score = (t ** 0.55) * (f_adj ** 0.45)

    # 3) 관계 조정 (원신호 재계산이 아니라 '두 점수의 일치도'만 본다)
    gap = abs(tech_score - f_adj)
    if tech_score >= 65 and f_adj >= 65:
        score += 6          # 차트·가치 모두 양호 → 고확신
    elif tech_score >= 55 and f_adj >= 55:
        score += 2
    if gap >= 35:
        score -= 6          # 한쪽만 좋음 → 신호 충돌, 신뢰 저하
    elif gap >= 22:
        score -= 3

    # 4) 안전 레일: 강한 하락추세만 하드캡 (리스크 오버레이)
    if regime == 'strong_down':
        score = min(score, 40)
    score = int(max(0, min(100, round(score))))

    # 신뢰도 등급 (데이터 완전성 + 두 점수 일치도)
    conf = fund_conf * (1 - min(gap, 50) / 100)
    conf_label = '높음' if conf >= 0.6 else ('보통' if conf >= 0.35 else '낮음')

    # 판정/근거 (사도 될지 말지 한 줄)
    if score >= 72:
        verdict, vlabel, vemoji = 'strong_buy', '적극 매수 고려', '🟢'
    elif score >= 60:
        verdict, vlabel, vemoji = 'buy', '분할 매수 고려', '🟢'
    elif score >= 48:
        verdict, vlabel, vemoji = 'watch', '관망', '🟡'
    else:
        verdict, vlabel, vemoji = 'avoid', '매수 보류', '🔴'

    if regime == 'strong_down':
        why = '강한 하락추세 — 반등 확인 전 매수 자제(떨어지는 칼날)'
    elif rr is not None and rr < 1.0:
        why = '현재가는 손익비 불리 — 눌림목(저점) 대기'
    elif tech_score >= 60 and f_adj >= 60:
        why = '차트·가치 모두 양호 — 정석 매수 구간'
    elif tech_score - f_adj >= 22:
        why = '흐름은 좋으나 밸류 부담 — 단기 트레이딩 한정·비중 축소'
    elif f_adj - tech_score >= 22:
        why = '저평가 우량주이나 타이밍 미흡 — 분할매수·바닥 확인 후'
    elif tech_score < 48 and f_adj < 48:
        why = '차트·가치 모두 부진 — 매수 보류'
    elif score >= 60:
        why = '차트가 받쳐주나 밸류는 평범 — 분할 접근 권장'
    else:
        why = '뚜렷한 우위 없음 — 관망 권장'
    if fund_conf < 0.5:
        why += ' (가치 데이터 부족 → 차트 위주 해석)'

    return {'score': score, 'verdict': verdict, 'verdict_label': vlabel,
            'verdict_emoji': vemoji, 'why': why,
            'confidence': round(conf, 2), 'confidence_label': conf_label}


