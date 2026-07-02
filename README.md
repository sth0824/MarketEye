# MarketEye — 기업 비교 대시보드

야후 파이낸스(yfinance)와 KRX 상장 종목 데이터를 기반으로 국내·해외 주식을
한눈에 비교하는 대시보드입니다. 카드/비교 테이블/차트 3가지 뷰와 한국어·종목코드
검색, 30초 자동 가격 새로고침을 지원합니다.

## 구성

- `app.py` — Flask 백엔드 (yfinance 조회, KRX 종목 검색, TTL 캐시)
- `index.html` — 단일 페이지 프론트엔드 (Chart.js)

## 실행

```bash
pip install -r requirements.txt
python3 app.py            # http://localhost:5001 에서 API 제공
```

그런 다음 `index.html`을 브라우저로 열면 됩니다.

### 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `PORT` | `5001` | API 서버 포트 |
| `FLASK_DEBUG` | `0` | `1`이면 디버그 모드 (개발 전용) |
| `STOCK_TTL` | `60` | 전체 종목 데이터 캐시 TTL(초) |
| `PRICE_TTL` | `15` | 가격 전용 캐시 TTL(초) |

## API

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /api/search?q=` | 종목 검색 (KRX 로컬 + 야후 글로벌) |
| `GET /api/stock/<ticker>` | 종목 전체 데이터 (재무·밸류에이션·히스토리) |
| `GET /api/price/<ticker>` | 가격·등락률만 (자동 새로고침용 경량) |
| `GET /api/batch?tickers=A,B` | 여러 종목 병렬 조회 |

## 배포 (Render 무료 플랜) — 서버가 잠들지 않게 유지

Render 무료 웹서비스는 **15분간 외부 요청이 없으면 서버를 재웁니다(spin-down)**.
다시 깨어나는 데 30~60초가 걸려, 그 사이 접속하면 브라우저가 "사이트에 연결할 수
없음 / 응답 시간 초과"로 실패합니다.

이를 막기 위해 `.github/workflows/keepalive.yml` 가 **5분마다 `/api/health` 를 호출**해
서버를 계속 깨워 둡니다. 이 리포는 public 이라 GitHub Actions 사용 분은 무료입니다.
설정할 것은 없으며, 리포에 push 되면 자동으로 동작합니다.

- **서비스 URL이 바뀌면**: 코드 수정 없이 리포 Variable 로 덮어씁니다.
  `Settings → Secrets and variables → Actions → Variables → New repository variable`
  → 이름 `KEEPALIVE_URL`, 값 `https://<서비스>.onrender.com/api/health`
- **동작 확인**: 리포의 `Actions → Keep Render awake` 탭에서 실행 이력·성공 여부 확인.
  즉시 한 번 깨우려면 `Run workflow` 버튼으로 수동 실행.
- **콜드스타트 자체를 더 튼튼히**: Render 서비스의 Start Command 를
  `gunicorn app:app --timeout 120 --workers 1 --threads 4` 로 두면(첫 요청이 야후
  응답을 기다리다 워커가 죽는 일 방지), Health Check Path 는 `/api/health` 로 둔다.

> 참고: GitHub Actions 스케줄은 부하 시 지연될 수 있어(특히 정시 근처) 5분 주기로
> 여유를 뒀습니다. 더 확실히 하려면 [cron-job.org](https://cron-job.org),
> UptimeRobot 같은 외부 모니터로 `/api/health` 를 함께 핑해도 됩니다.

## 참고

- 야후 파이낸스 비공식 데이터를 사용하므로 과도한 호출 시 일시 차단될 수 있습니다.
  TTL 캐시로 완화하지만, 종목 수가 많으면 새로고침 주기를 늘리는 것을 권장합니다.
- 통화가 다른 종목(예: ₩ 삼성전자 vs $ 애플)의 금액성 지표(시총·EPS 등)는
  단순 비교가 무의미하므로 비교 테이블에서 우열 하이라이트를 생략합니다.
