# KORU 자동매매 시스템

미국 상장 3배 레버리지 ETF **KORU**(Direxion Daily MSCI South Korea Bull 3X)를
한국투자증권 Open API 로 자동 매매하는 시스템.

**모든 손익 판정이 원화(KRW) 기준**이라는 점이 이 프로젝트의 핵심이다.
USD 가격이 올라도 환율이 빠지면 원화로는 손실일 수 있으므로,
익절·손절은 환율·수수료·환전스프레드를 전부 반영한 원화 실현수익률로 판정한다.

---

## ⚠️ 먼저 읽을 것

이 소프트웨어는 **교육·연구 목적**이며 투자 자문이 아니다.

- KORU 는 **일일 3배 레버리지** 상품이다. 기초지수가 -10% 면 -30% 다.
- 2019~2026년 KORU 의 **연간 최대낙폭은 매년 -43% ~ -83%** 였다.
- 이 저장소에 포함된 백테스트 결과는 **실거래 성과를 보장하지 않는다.**
- 기본값은 `DRY_RUN=true`, `KIS_ENV=paper`(모의투자)다. 실거래는 명시적으로 켜야 한다.
- **모의투자로 최소 수 주간 검증하지 않고 실거래에 올리지 마라.**

---

## 무엇을 하는가

| 기능 | 명령 | 설명 |
|---|---|---|
| **진입 판정** | `koru gate` | "지금 들어가도 되나" 를 3가지 증거로 종합 판정 |
| 백테스트 | `koru backtest` | 과거 성과 (원화 기준) |
| 전방 시뮬레이션 | `koru scan` | "아무 날에나 샀다면?" 결과 분포 |
| 강건성 검증 | `koru walkforward` | 워크포워드 + 몬테카를로 신뢰구간 |
| 민감도 분석 | `koru sweep` | 파라미터를 흔들면 결과가 얼마나 변하나 |
| 실거래 1틱 | `koru tick` | 판단 → 주문 → 상태저장 (기본 DRY_RUN) |
| 상태 조회 | `koru status` | 현재 포지션·리스크 카운터 |
| 설정 점검 | `koru doctor` | 자격증명 유무 확인 (값은 마스킹) |

---

## 전략 요약

### 분할 매수 (스케일인)

총 예산을 **3차 40% / 35% / 25%** 로 나눈다. 1차가 가장 크다 (마틴게일의 반대).
2·3차 트리거는 1차 진입가에서 **ATR 배수만큼 아래**(기본 0.7 ATR, 1.4 ATR)다.
고정 퍼센트가 아니라 ATR 기준이므로 변동성 레짐이 바뀌면 자동으로 조정된다.

추가 매수에는 두 가지 안전장치가 있다.

- **손절선 아래에서는 절대 사지 않는다.** 사자마자 손절당하는 구조를 막는다.
- **음봉 중에는 받지 않는다.** 종가가 시가보다 낮으면 하락이 진행 중이다.

### 분할 익절 (원화 기준)

| 단계 | 원화 수익률 | 매도 비율 | 부가 효과 |
|---|---|---|---|
| 1단 | **+5%** | 30% | 이후 손절선이 **원화 본전**으로 상향 |
| 2단 | **+9%** | 30% | 이후 트레일링 스톱 작동 |
| 3단 | **+14%** | 25% | |
| 4단 | **+20%** | 15% (잔량 전부) | |

1단 익절 이후 이 매매는 **원화 기준으로 손실이 날 수 없다**(본전 스톱).

### 손절 / 청산

| 조건 | 기본값 | 비고 |
|---|---|---|
| USD 하드 스톱 | 1차 진입가 − 2.0×ATR (4~12% 클램프) | **평단이 아니라 1차 진입가 기준** |
| 원화 하드 스톱 | 원화 평가수익률 −10% | 환율 급락 시 이쪽이 먼저 걸린다 |
| 본전 스톱 | 1단 익절 후 | 원화 본전가 |
| 트레일링 | 2단 익절 후, 최고 수익률의 35% 반납 | |
| 타임 스톱 | **5영업일** | 레버리지 감쇠 회피 |

### 진입 필터 (전부 통과해야 매수)

1. **변동성 레짐** — ATR(14)/종가 ≤ 8%
2. **추세 방향** — 종가 > EMA10 > EMA30 (정배열)
3. **모멘텀** — RSI(14) 45~70
4. **추세 강도** — ADX(14) ≥ 18
5. **시가 갭** — |갭| ≤ 5%
6. **유동성** — 20일 평균 거래대금 ≥ $3M
7. **환율 추세** — USD/KRW 20일 변화율 ≥ −3%

### 킬스위치

일일 손실 한도 30만원 / 연속 손절 3회 / 일일 진입 3회 / 1회 투입 500만원.
**한도에 걸리면 진입은 막히지만 청산은 절대 막히지 않는다.**

---

## 설치

```bash
git clone <repo-url> Koru_Trade
cd Koru_Trade

python -m venv .venv --system-site-packages
.venv/Scripts/python -m pip install -e ".[dev,data]"    # Windows
# .venv/bin/python -m pip install -e ".[dev,data]"      # macOS/Linux

git config core.hooksPath .githooks     # 하네스 게이트 활성화

cp .env.example .env                     # 자격증명 입력
```

`make setup` 한 줄로도 된다.

### 자격증명

[한국투자증권 API 포털](https://apiportal.koreainvestment.com)에서 앱키를 발급받아
`.env` 를 채운다. **`.env` 는 `.gitignore` 에 있어 절대 커밋되지 않는다.**

```bash
python -m koru_trade doctor    # 설정 확인 (값은 마스킹되어 출력된다)
```

---

## 사용 예

```bash
# 지금 들어가도 되는지 판정 (네트워크로 최신 시세를 받는다)
python -m koru_trade gate

# 고정 스냅샷으로 재현 가능한 백테스트 (네트워크 미사용)
python -m koru_trade backtest --csv tests/fixtures/koru_3y.csv --trades

# 강건성 검증
python -m koru_trade walkforward
python -m koru_trade sweep

# 실거래 1틱 (기본 DRY_RUN — 주문이 나가지 않는다)
python -m koru_trade tick

# 페이퍼 브로커로 리허설 (자격증명 불필요)
python -m koru_trade tick --paper
```

---

## 개발

**소스를 고쳤으면 `python scripts/verify.py` 가 전부 통과해야 완료다.**
자세한 규약은 [AGENTS.md](AGENTS.md) 를 보라.

```bash
make verify        # 전체 게이트 (secrets → format → lint → types → tests)
make verify-fast   # 빠른 확인
make fix           # 자동 수정 후 재검증
```

게이트는 세 겹으로 강제된다.

1. `.githooks/pre-commit` — 커밋 차단
2. `.githooks/pre-push` — 푸시 차단
3. `.github/workflows/ci.yml` — 머지 차단 (+ gitleaks 시크릿 스캔)

---

## 프로젝트 구조

```
src/koru_trade/
├── pnl.py             원화 손익 계산 (이 프로젝트의 심장)
├── strategy.py        매매 판단 (순수 함수)
├── risk.py            킬스위치
├── entry_gate.py      "지금 진입해도 되나" 판정
├── indicators.py      기술적 지표
├── models.py          불변 값 객체
├── config.py          전략 설정(공개) / 자격증명(비공개) 분리
├── backtest/          엔진 · 지표 · 전방시뮬 · 워크포워드
├── broker/            KIS REST · 페이퍼 · 유량제한
└── live/              실행 루프 · SQLite 상태
```

백테스트와 실거래는 **같은 `strategy.decide()`** 를 호출한다.
`decide()` 는 네트워크·파일·시계를 건드리지 않는 순수 함수이므로
백테스트에서 검증한 동작이 실거래에서 달라질 수 없다.

---

## 보안

이 저장소는 GitHub 공개를 전제로 만들어졌다.

- 앱키·앱시크릿·계좌번호는 **환경변수에서만** 읽는다. 설정 파일에 자리가 없다.
- `Credentials.__repr__` 이 마스킹되어 `print(cred)` 로도 유출되지 않는다.
- API 예외 메시지에 요청 헤더·바디를 담지 않는다.
- `tests/test_security.py` 가 **커밋 대상 파일을 실제로 읽어서** 시크릿 패턴을 검사하며,
  이 검사가 `verify.py` 의 첫 게이트다.
- CI 에서 `gitleaks` 가 전체 커밋 이력을 추가 스캔한다.

공개 저장소에서는 커밋 메타데이터의 이메일도 노출된다. GitHub noreply 사용을 권한다.

```bash
git config user.email "<숫자ID>+<사용자명>@users.noreply.github.com"
```

---

## 라이선스

MIT. 자세한 내용은 [LICENSE](LICENSE) 참조.

이 소프트웨어로 발생한 손실에 대해 작성자는 어떤 책임도 지지 않는다.
