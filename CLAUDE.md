# MarketEye — Claude 작업 지침

## 응답 규칙
- 코드를 변경할 때마다 **항상 커밋 메시지를 함께 제안**할 것.
- 커밋 메시지는 기존 히스토리 컨벤션을 따른다:
  - 형식: `<type>: <한국어 설명>`
  - type: `feat`(기능 추가), `fix`(버그 수정), `style`(스타일/UI), `refactor`, `docs`, `chore`
  - 예) `fix: 신호 탭 PER/PBR이 한국 종목에서 안 뜨던 문제 수정`

## 프로젝트 개요
- 국내·해외 주식 비교 대시보드. yfinance + KRX 종목 데이터 기반.
- `app.py` — Flask 백엔드 (yfinance 조회, KRX 검색, TTL 캐시, Supabase 동기화)
- `index.html` — 단일 파일 프론트엔드 (Chart.js, 다크 테마)
- `krx_stocks.json` — KRX 종목 폴백 번들
