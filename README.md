# 단타 스크리너 (Conditional Uplift)

매 평일 한국시간 08:30에 자동으로 단타 후보 2~3개를 추려 텔레그램으로 전송합니다.

## 동작 원리

1. **유니버스**: 한국 시총 상위 ~200개 (시총 1,000억↑) + 미국 드라이버 ~25개 (NVDA/TSLA/TSM 등 대형주 + SMH/XLE/URA 등 섹터 ETF)
2. **모델**: 각 한국 종목에 대해 최근 250 거래일 동안 `KR_return[t] = α + β · US_return[t-1] + ε` 회귀로 베타 추정. 종목별로 상관계수 절대값이 큰 미국 드라이버 Top-3 선택.
3. **점수**: 어젯밤 큰 움직임(|≥1.5%|)이 있었던 US 드라이버들의 베타 × 움직임 합산 → 예상 익일 수익률
4. **필터**: 시총 1,000억↑, 최근 거래량 ≥ 20일 중앙값 × 1.5
5. **출력**: 예상 수익률 상위 2~3개 + 어떤 미국 드라이버가 얼마나 기여했는지 설명

## 실행

- 자동: GitHub Actions `morning-picks` 워크플로 (`.github/workflows/morning_picks.yml`)
- 수동: Actions 탭에서 `Run workflow` 또는 `gh workflow run morning-picks`

## 필요한 Secrets

- `TELEGRAM_BOT_TOKEN`: BotFather에서 받은 봇 토큰
- `TELEGRAM_CHAT_ID`: 메시지 받을 채팅 ID

## 파라미터 변경

`universe.py`의 상수들로 조정:
- `KR_UNIVERSE_SIZE`: 후보 유니버스 크기
- `KR_MIN_MARKET_CAP_KRW`: 최소 시총
- `US_MOVE_THRESHOLD_PCT`: 이만큼 못 움직인 US 드라이버는 무시
- `MIN_VOLUME_RATIO`: 거래량 필터
- `TOP_K_PICKS`: 최종 후보 개수
- `TOP_K_DRIVERS_PER_STOCK`: 종목당 사용할 드라이버 수

## 면책

통계 모델 스크리닝일 뿐 매매 추천이 아닙니다. 실제 매매 전 호가/뉴스/공시 직접 확인 필수.
