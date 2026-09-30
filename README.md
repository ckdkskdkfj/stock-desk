# stock-desk

종목 연구 데스크의 데이터 파이프라인. GitHub Actions가 매일 자동으로 돌아 `data/`에 결과를 남깁니다.

| 작업 | 시각(한국) | 결과 |
|---|---|---|
| KR 60일 신고가 | 평일 16:07 | `data/kr/latest.json`, `data/kr/universe.json`, `data/status_kr.json` |
| US 52주 신고가 | 화~토 06:37 | `data/us/latest.json`, `data/us/universe.json`, `data/status_us.json` |

- 국내: 시총 1조·거래대금 50억·주가 2,000원·등락 +3% 이상 풀 → 종가가 직전 60거래일 최고가를 넘으면 신고가. RS 등급은 시총 1조 이상 전 종목의 60거래일 수익률 백분위(1~99). 시세 경로: 한국거래소(pykrx) → 네이버(FinanceDataReader) → 네이버 모바일 API 순으로 예비 전환.
- 미국: 시총 10억달러 이상 전 종목 → 당일 고가가 직전 250거래일 최고가 이상이면 52주 신고가. RS 등급 동일 방식.
- 대시보드 회차(Claude)는 이 파일만 읽고 조사·작성만 합니다. 실행 기록은 `data/logs/`.
