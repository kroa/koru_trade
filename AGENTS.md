# AGENTS.md — KORU 자동매매 저장소 작업 규약

이 문서는 **사람과 AI 코딩 에이전트 모두**를 위한 이 저장소의 작업 계약이다.
Claude Code, Codex, Cursor 등은 작업 시작 시 이 파일을 읽는다.
`CLAUDE.md` 는 이 파일을 가리키는 포인터일 뿐이며, 규약의 원본은 여기다.

---

## 0. 이 저장소가 다루는 것

미국 상장 3배 레버리지 ETF **KORU**(Direxion Daily MSCI South Korea Bull 3X)를
한국투자증권 Open API 로 자동 매매하는 시스템이다.

핵심은 단 하나다. **모든 손익 판정은 원화(KRW) 기준이다.**
USD 가격이 올라도 환율이 빠지면 원화로는 손실일 수 있다.
이 프로젝트의 익절/손절은 전부 `koru_trade.pnl` 이 계산한 원화 실현수익률로 판정한다.

**실제 돈이 움직이는 코드다.** 버그 하나가 3배 레버리지로 증폭된다.
아래 규약은 권고가 아니라 강제 사항이다.

---

## 1. 하네스 규약 — "완료" 의 정의

> ### 소스를 수정했으면 `python scripts/verify.py` 가 전부 통과해야 완료다.
> 통과하지 않았다면 그 작업은 **아직 진행 중**이다. 예외는 없다.

이것을 사람의 의지에 맡기지 않는다. 세 겹으로 강제한다.

| 층 | 메커니즘 | 파일 |
|---|---|---|
| 1 | 로컬 커밋 차단 | `.githooks/pre-commit` |
| 2 | 로컬 푸시 차단 | `.githooks/pre-push` |
| 3 | 원격 머지 차단 | `.github/workflows/ci.yml` |

Claude Code 사용자는 `.claude/settings.json` 의 `PostToolUse` 훅이
파일을 고칠 때마다 빠른 게이트를 자동 실행한다.

### 최초 1회 설정

```bash
python -m venv .venv --system-site-packages
.venv/Scripts/python -m pip install -e ".[dev,data]"   # Windows
# .venv/bin/python -m pip install -e ".[dev,data]"     # macOS/Linux
git config core.hooksPath .githooks
```

### 검증 게이트

```bash
python scripts/verify.py           # 전체 (커밋 전 필수)
python scripts/verify.py --fast    # 타입체크·커버리지 생략 (개발 중)
python scripts/verify.py --fix     # 자동 수정 가능한 것 고치고 진행
python scripts/verify.py lint      # 특정 게이트만
```

| 게이트 | 검사 내용 | 실패 시 의미 |
|---|---|---|
| `secrets` | 비밀정보·개인정보가 커밋 대상에 있는지 | **즉시 중단.** 절대 우회 금지 |
| `format` | `ruff format` 규칙 | `--fix` 로 자동 해결 |
| `lint` | `ruff check` (pycodestyle/bugbear/bandit 포함) | 대부분 `--fix` 로 해결 |
| `types` | `mypy --strict` | 타입 주석을 고쳐라. `Any` 로 도망가지 마라 |
| `tests` | `pytest` + **커버리지 85% 이상** | 아래 2절 참조 |

### 우회 금지

`git commit --no-verify` 는 원칙적으로 금지다.
불가피하게 썼다면 **커밋 메시지 본문에 사유와 후속 조치를 반드시 적어라.**
`secrets` 게이트는 어떤 경우에도 우회하지 않는다.

---

## 2. 테스트 규약

### 변경 유형별 필수 테스트

| 무엇을 고쳤나 | 무엇을 반드시 해야 하나 |
|---|---|
| `pnl.py` (원화 손익) | 수학적 항등식 테스트 추가. 역함수 왕복 검증 필수 |
| `strategy.py` (매매 판단) | 새 분기마다 테스트. **판정 우선순위**가 바뀌면 순서 테스트 갱신 |
| `risk.py` (킬스위치) | 한도 초과 시 **차단되는지**와 청산은 **여전히 되는지** 둘 다 |
| `backtest/` | 결정론·현금보존·look-ahead 없음을 재확인 |
| `broker/kis.py` | `responses` 로 모킹. **실제 서버를 부르는 테스트 금지** |
| `config.py` | 왕복 직렬화 + 알 수 없는 키 거부 |
| `web/` | 루프백 바인딩 유지 + JSON 에 NaN 없음 + 자격증명 미포함 |
| `notify/` | 전송 실패가 예외로 새지 않는지 + 토큰이 로그·예외에 없는지 |
| 무엇이든 | `python scripts/verify.py` 전체 통과 |

### 절대 하지 말 것

- **테스트를 통과시키려고 단언을 느슨하게 만들지 마라.** 코드를 고쳐라.
  단언이 틀렸다는 근거가 있으면, 왜 틀렸는지를 테스트 docstring 에 적고 고쳐라.
- **커버리지 임계값을 낮추지 마라.** (`pyproject.toml` 의 `--cov-fail-under=85`)
- **테스트에서 네트워크를 쓰지 마라.** 실데이터가 필요하면
  `tests/fixtures/koru_3y.csv` 스냅샷을 쓴다.
- **`pytest.ini` 나 `pyproject.toml` 의 `addopts` 에서 `--cov` 를 빼지 마라.**

### 테스트 작성 스타일

- 테스트 이름은 **한국어 서술문**으로 쓴다: `test_환율이_내려가면_필요가격이_올라간다`.
  무엇을 보장하는지가 이름만으로 읽혀야 한다.
- 불변식(invariant) 테스트를 선호한다. 특정 숫자보다 "합이 보존된다", "순서가 유지된다" 가 강하다.
- 금융 로직 테스트에는 **왜 이 값이어야 하는지**를 docstring 에 적는다.

---

## 3. 보안 규약 — 이 저장소는 GitHub 에 공개된다

### 절대 커밋하지 않는 것

- 한국투자증권 앱키 / 앱시크릿 (`KIS_APP_KEY`, `KIS_APP_SECRET`)
- 계좌번호 (`KIS_ACCOUNT_NO`, `CANO`)
- 접근 토큰, 해시키
- 실거래 상태 DB (`state/*.db`) — 체결 이력과 보유 내역이 들어 있다
- 로그 파일 (`logs/`, `*.log`)
- 개인 이메일 주소, 실명, 전화번호

### 강제 방법

1. `.gitignore` 가 위 항목을 전부 막는다.
2. `tests/test_security.py` 가 **커밋 대상 파일을 실제로 읽어서** 패턴 검사한다.
   - KIS 앱키 형식(`PS` + 34자)
   - 100자 이상 시크릿 형식 문자열
   - `CANO=`/`account_no=` 뒤의 숫자 리터럴
   - 이메일 주소 (`example.com`, `noreply.github.com` 만 허용)
   - `Bearer <토큰>` 리터럴
3. `verify.py` 의 **첫 번째 게이트**가 이 검사다. 가장 먼저 실패한다.
4. GitHub Actions 가 `gitleaks` 를 추가로 돌린다.

### 코드 작성 규칙

```python
# 이렇게 한다
cred = load_credentials()               # 환경변수에서만 읽는다
logger.info("계정 %s", mask_secret(key)) # 항상 마스킹
raise BrokerError(f"실패 (HTTP {code})") # 상태 코드만

# 절대 이렇게 하지 않는다
APP_KEY = "PS1234..."                    # 하드코딩
logger.debug("headers=%s", headers)      # 헤더에 키가 들어 있다
raise BrokerError(f"실패: {response.text}")  # 응답에 요청이 반사될 수 있다
```

- `Credentials.__repr__` 은 마스킹되어 있다. **절대 풀지 마라.**
- 테스트에 쓰는 가짜 값에는 `FAKE`/`EXAMPLE` 같은 표식을 넣어라.
  안 그러면 시크릿 스캐너가 정상적으로 잡아낸다(그게 맞는 동작이다).

### 커밋 작성자 이메일

공개 저장소에서는 커밋 메타데이터의 이메일도 노출된다. GitHub noreply 사용을 권한다.

```bash
git config user.email "<숫자ID>+<사용자명>@users.noreply.github.com"
```

---

## 4. 아키텍처 — 어디를 고쳐야 하나

```
src/koru_trade/
├── models.py          값 객체만. 로직 없음. 전부 frozen dataclass
├── pnl.py             ★ 원화 손익 계산. 이 프로젝트의 심장
├── indicators.py      기술적 지표. 순수 함수, 외부 의존성 없음
├── config.py          전략 설정(공개) / 자격증명(비공개) 분리
├── risk.py            ★ 킬스위치와 한도. 진입은 막고 청산은 안 막는다
├── strategy.py        ★ 매매 판단. 부작용 없는 순수 함수
├── entry_gate.py      "지금 진입해도 되나" 종합 판정
├── cli.py             명령줄 인터페이스
├── data/loader.py     시세·환율 적재. 반드시 조정주가
├── backtest/
│   ├── engine.py      이벤트 기반 백테스터
│   ├── metrics.py     성과 지표
│   ├── pathsim.py     전방 경로 시뮬레이션
│   └── walkforward.py 워크포워드 / 몬테카를로 / 민감도
├── broker/
│   ├── base.py        브로커 프로토콜
│   ├── kis.py         한국투자증권 REST 클라이언트
│   ├── paper.py       페이퍼 브로커
│   └── ratelimit.py   유량 제한 + 재시도
├── live/
│   ├── runner.py      실거래 루프
│   └── state.py       SQLite 상태 영속화
├── web/
│   ├── snapshot.py    대시보드 데이터 조립 (HTTP 를 모르는 순수 로직)
│   ├── server.py      로컬 HTTP 서버 (표준 라이브러리만)
│   └── static/        dashboard.html (외부 리소스 0개)
└── notify/
    ├── format.py      알림 본문 (순수 함수)
    ├── telegram.py    텔레그램 채널
    └── base.py        Notifier 프로토콜 + NullNotifier
```

### 지켜야 할 구조적 불변식

1. **`strategy.decide()` 는 순수 함수다.** 네트워크·파일·`datetime.now()` 금지.
   시각이 필요하면 인자로 받는다. 이것이 백테스트와 실거래가 같은 코드를 쓰는 근거다.
2. **백테스트와 실거래는 같은 `decide()` 를 부른다.** 한쪽에만 로직을 넣지 마라.
3. **`models.py` 에 로직을 넣지 마라.** 값 객체와 검증만.
4. **손절선은 1차 진입가 기준이다.** 평단 기준으로 바꾸면 분할매수마다
   손절선이 따라 내려가 손실 한도가 사라진다.
5. **비용은 `CostModel` 한 곳에서만 정의한다.** 수수료율을 다른 곳에 적지 마라.

---

## 5. 도메인 함정 — 여기서 실수하면 돈을 잃는다

### 조정주가

KORU 는 **2025-02-10 에 1:10 역분할**, **2026-07-15 에 20:1 액면분할**을 했다.
미조정 주가로 백테스트하면 분할일에 ±90% 짜리 가짜 봉이 생겨 결과 전체를 지배한다.
`data/loader.py` 는 `auto_adjust=True` 로만 데이터를 받는다. **끄지 마라.**
`tests/test_config_and_data.py::test_분할_구간에_비현실적_점프가_없다` 가 이를 지킨다.

### 거래소 코드 불일치

KIS 해외주식 API 는 **시세 조회와 주문의 거래소 코드가 다르다.**

| 거래소 | 시세 조회 | 주문 |
|---|---|---|
| NYSE Arca (KORU) | `AMS` | `AMEX` |
| NASDAQ | `NAS` | `NASD` |
| NYSE | `NYS` | `NYSE` |

이 연동에서 가장 흔한 버그다. `broker/kis.py` 의 `EXCHANGE_QUOTE_CODE` /
`EXCHANGE_ORDER_CODE` 두 표를 반드시 구분해서 쓴다.

**KORU 는 `AMS`(아멕스)다.** KIS 종목마스터 `AMSMST.COD` 에 등록되어 있으며
`NYSMST.COD` / `NASMST.COD` 에는 없다 (NASDAQ 의 "KORU" 는 티커 `KRMD` 인 별개 종목).
NYSE Arca ETF 는 대체로 `AMS` 로 분류된다: SPY/SOXL/ARKK/IWM → `AMS`, QQQ/TQQQ → `NAS`.

### 모의투자 TR_ID 비대칭 — 가장 잡기 어려운 함정

| 동작 | 실전 | 모의 |
|---|---|---|
| 미국 매수 | `TTTT1002U` | `VTTT1002U` |
| **미국 매도** | `TTTT1006U` | **`VTTT1001U`** |
| 정정/취소 | `TTTT1004U` | `VTTT1004U` |
| 잔고 | `TTTS3012R` | `VTTS3012R` |
| 체결내역 | `TTTS3035R` | `VTTS3035R` |
| 미체결 | `TTTS3018R` | `VTTS3018R` |
| 매수가능금액 | `TTTS3007R` | `VTTS3007R` |

매도만 규칙이 깨진다. KIS 공식 샘플의 `tr_id = "V" + tr_id[1:]` 일괄 치환 규칙을
그대로 쓰면 존재하지 않는 `VTTT1006U` 를 보내게 된다.
`tests/test_broker.py::test_모의투자_매도_TR_ID가_비대칭이다` 가 이를 고정한다.

### API 유량 제한은 숫자로 고지되지 않는다

KIS 공식 문서는 모의투자 한도를 "실전보다 낮습니다" 라고만 적는다.
2차 자료들은 초당 1건/2건/5건으로 서로 어긋난다.
따라서 **특정 숫자를 상수로 믿지 마라.** `EGW00201`(초당 거래건수 초과)을 받으면
`KisBroker._throttle_down()` 이 한도를 절반으로 낮춘다. 이 적응형 동작을 제거하지 마라.

### 주문 가능 금액은 추정하지 않는다

로컬에서 "현금 − 기제출 주문" 을 계산하면 미결제 금액, 환전 대기, 증거금 규칙 때문에
실제와 어긋난다. `KisBroker.buying_power_usd()` 로 브로커에 직접 물어본다.

### MGCO_APTM_ODNO 를 멱등키로 신뢰하지 마라

KIS 주문 API 에 이 파라미터가 있지만, **서버가 중복을 거부한다는 근거가 문서에 없다.**
멱등성은 로컬(`live/state.py` 의 `submitted_orders` 테이블 + `client_order_id`)에서 보장한다.

### 변동성 감쇠

3배 레버리지 상품은 기초지수가 제자리로 돌아와도 손실이 남는다.
연율 감쇠는 대략 `-0.5 x (L² - L) x σ²` 이며, L=3, σ=50% 면 **연 -37.5%** 다.
`indicators.leverage_decay_estimate()` 가 이를 추정한다.
**보유 기간을 늘리는 방향의 변경은 이 비용을 반드시 근거로 반박해야 한다.**

### NaN

`NaN` 은 모든 비교가 `False` 라서 `if value <= 0` 검사를 그냥 통과한다.
시세 API 나 pandas 결측치가 그대로 흘러들어오면 백테스트 전체가 조용히 오염된다.
`Bar.__post_init__` 이 `math.isfinite()` 로 먼저 막는다. **이 검사를 지우지 마라.**

### 알림은 매매를 막을 수 없다

`SignalNotifier.send()` 는 **어떤 경우에도 예외를 던지지 않는다.**
텔레그램이 죽어도, 토큰이 만료돼도, 네트워크가 끊겨도 매매 루프는 계속 돌아야 한다.
`LiveRunner._notify()` 가 한 겹 더 감싸는 이유도 같다.
반대 방향은 성립하지 않는다 — 알림을 못 보내서 주문을 못 내는 상황은 없어야 한다.

봇 토큰은 API URL 경로(`/bot<TOKEN>/sendMessage`)에 들어간다.
requests 예외 문자열에는 URL 이 통째로 들어 있으므로 **예외를 그대로 올리면
토큰이 로그에 남는다.** 그래서 타입 이름만 남기고 삼킨다.

### 검사 설정 파일도 검사한다

깨진 `.gitleaks.toml` 은 오류를 내지 않고 **조용히 아무것도 검사하지 않는다.**
보호받고 있다고 착각하는 것이 보호가 없는 것보다 나쁘다.
`tests/test_security.py::TestScannerConfigIntegrity` 가 TOML 파싱, 정규식 컴파일,
제어문자 혼입, 그리고 **표본으로 실제 탐지되는지**까지 확인한다.

### 대시보드는 루프백에만 연다

포지션과 손익은 개인 금융정보다. `DashboardServer` 는 루프백이 아닌 host 를
**거부한다**. 편의를 위해 `0.0.0.0` 으로 여는 변경은 받지 않는다.

Windows 의 `SO_REUSEADDR` 은 유닉스와 의미가 달라서 **이미 LISTEN 중인 포트에도
바인딩이 성공한다.** 빈 포트를 찾는다면서 사용자가 띄워 둔 다른 프로그램의 포트를
빼앗게 된다. `_apply_exclusive()` 가 `SO_EXCLUSIVEADDRUSE` 를 걸어 이를 막는다.
`is_port_free()` 는 bind 와 connect 를 **둘 다** 검사한다. 하나만으로는 부족하다.

### JSON 에 NaN 을 흘리지 마라

파이썬 `json.dumps` 는 `NaN`/`Infinity` 를 그대로 뱉지만 브라우저의 `JSON.parse` 는
이를 거부한다. 값 하나 때문에 대시보드 전체가 백지가 된다.
`snapshot_to_json()` 이 `allow_nan=False` 와 `_sanitize()` 로 막는다.
Profit Factor 는 전승 시 무한대가 되므로 실제로 발생하는 경로다.

### 파라미터를 바꿀 때는 워크포워드까지 본다

단일 백테스트 수익률만 보고 파라미터를 고르면 반드시 곡선 맞추기가 된다.
실제로 겪은 예: `min_adx` 를 0 으로 내리면 3년 누적이 +12% -> +19% 로 오르지만
워크포워드 수익구간이 3/4 -> 2/4 로 떨어진다. 수익률이 올랐는데 더 나쁜 설정이다.

파라미터 기본값을 바꾸는 변경에는 **백테스트 + 워크포워드 + 부트스트랩 신뢰구간**
세 가지를 함께 제시하라. 신뢰구간이 0을 포함하면 "입증되지 않았다" 고 명시하라.

`config/daytrade.example.yaml` 이 이 절차를 따른 예다.

### 익절 기준 수량

익절 계단의 매도 비율은 "잔여 수량" 이 아니라 `Position.ladder_base_qty` 기준이다.
잔여 기준으로 바꾸면 계단마다 실제 매도량이 달라져 사전 검증이 불가능해진다.

---

## 6. 실거래 안전 규약

- `KORU_DRY_RUN` 의 **기본값은 `true`** 다. 환경변수가 없거나 이상하면 주문이 나가지 않는다.
  이 기본값을 뒤집는 변경은 절대 금지다.
- `KIS_ENV` 의 **기본값은 `paper`**(모의투자)다.
- 주문에는 항상 멱등키(`client_order_id`)를 붙인다. 같은 봉·같은 결정이면 같은 키가 나온다.
- **주문 제출은 자동 재시도하지 않는다.** 타임아웃 시 주문이 나갔는지 알 수 없으므로,
  재시도 대신 미체결 조회(`open_orders()`)로 확인한다.
- 모든 판단은 `state.log_decision()` 으로 감사 로그에 남긴다.
  "왜 그때 샀나" 에 답할 수 없는 자동매매는 운영하면 안 된다.

---

## 7. 커밋 규약

```
<타입>: <한 줄 요약>

<왜 이 변경이 필요한지>
<무엇이 바뀌는지>
<검증 방법: 어떤 테스트가 이를 지키는지>
```

타입: `feat` / `fix` / `refactor` / `test` / `docs` / `chore` / `perf`

전략 파라미터 기본값을 바꾸는 커밋에는 **백테스트 결과를 함께 적어라.**
숫자 없이 파라미터를 바꾸는 것은 근거 없는 변경이다.

---

## 8. 에이전트를 위한 체크리스트

작업을 마쳤다고 보고하기 전에 확인하라.

- [ ] `python scripts/verify.py` 를 실제로 실행했고 전부 통과했다
- [ ] 새로 만든 분기마다 테스트가 있다
- [ ] 커버리지가 85% 아래로 떨어지지 않았다
- [ ] 비밀정보·개인정보를 새로 추가하지 않았다
- [ ] `strategy.decide()` 의 순수성을 깨지 않았다
- [ ] 전략 파라미터를 바꿨다면 백테스트 숫자를 근거로 제시했다
- [ ] 실거래 기본값(`DRY_RUN=true`, `KIS_ENV=paper`)을 건드리지 않았다

> **테스트를 돌리지 않고 "완료" 라고 말하지 마라.**
> 이 저장소에서 검증되지 않은 완료 보고는 완료가 아니라 추측이다.
