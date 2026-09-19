"""상황실 페이지 생성기의 **데이터 계약** 테스트.

이 저장소에서 반복된 사고는 전부 "조용한 불일치" 였다. 페이지가 읽는 필드와
생성기가 넣는 필드가 어긋나면 브라우저는 오류를 내지 않고 그 자리만 비운다.
숫자가 틀린 것보다 나쁘다 — 틀렸다는 사실 자체를 모르기 때문이다.

그래서 **계약 목록을 이 파일에 적지 않는다.** 템플릿의 자바스크립트에서 직접
뽑아낸다. 나중에 페이지를 고쳐 새 필드를 읽게 되면, 생성기를 고치기 전에
이 테스트가 먼저 깨진다. 목록을 하드코딩하면 그 순간 계약이 둘로 갈라져
"테스트는 통과하는데 화면은 빈" 상태가 된다.

네트워크는 쓰지 않는다. ``build_payload`` 는 변동성을 인자로 받는 순수 함수라
가짜 봉만으로 그대로 부를 수 있다.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import make_series

from koru_trade import indicators as ind
from koru_trade.config import StrategyConfig
from koru_trade.models import Bar

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_desk  # noqa: E402
import build_site  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

FIXED_NOW = dt.datetime(2026, 9, 19, 21, 21, tzinfo=KST)
"""고정 기준 시각(토요일). 실행 날짜에 따라 결과가 바뀌면 계약 테스트가 아니다."""

EWY_VOL = 0.0391
"""기초지수 일간 변동성. 실제 관측 수준의 값을 네트워크 없이 고정한다."""

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S)
PAYLOAD_RE = re.compile(r'<script id="payload" type="application/json">(.*?)</script>', re.S)

TEMPLATES = [build_desk.TEMPLATE, build_site.TEMPLATE]
TEMPLATE_IDS = ["desk", "signals"]


# ---------------------------------------------------------------------------
# 템플릿에서 계약을 뽑아내는 도구
# ---------------------------------------------------------------------------


def _logic_script(template_text: str) -> str:
    """페이지 로직 스크립트만. payload 블록(``__DATA__``)은 제외한다.

    CSS 와 본문 한국어까지 긁으면 ``D.`` 같은 패턴에서 가짜 필드가 잡힌다.
    """
    bodies = [
        m.group("body")
        for m in SCRIPT_RE.finditer(template_text)
        if 'id="payload"' not in m.group("attrs")
    ]
    assert bodies, "템플릿에서 페이지 로직 스크립트를 찾지 못했다"
    return "\n".join(bodies)


def _fields_of(script: str, token: str) -> set[str]:
    """``token.<필드>`` 로 읽히는 이름 전부."""
    return set(re.findall(r"\b" + re.escape(token) + r"\.(" + IDENT + r")", script))


def _aliases(script: str) -> dict[str, str]:
    """``var C = D.cost`` 처럼 payload 하위를 별칭으로 받는 곳. {별칭: 최상위 키}.

    앞에 ``.`` 이 붙은 이름은 별칭이 아니라 대입 대상이다. 이 구분을 빠뜨리면
    ``$("checks").innerHTML = D.checks`` 에서 ``innerHTML -> checks`` 라는 가짜
    별칭이 생기고, 배열인 checks 를 사전으로 착각해 엉뚱한 곳에서 터진다.
    """
    found = re.findall(r"(?<![.\w$])(" + IDENT + r")\s*=\s*D\.(" + IDENT + r")", script)
    return dict(found)


def _callback_body(script: str, pos: int) -> str:
    """``pos`` 이후 첫 ``{`` 부터 짝이 맞는 ``}`` 까지.

    콜백마다 본문을 잘라 내는 이유는 **인자 이름이 겹치기 때문**이다. ``s`` 는
    ``R.scaleIn`` 콜백에도 쓰이고 상단 ``STAT`` 배열 콜백에도 쓰인다. 스크립트
    전체에서 ``s.`` 를 긁으면 scaleIn 원소에 STAT 의 필드까지 요구하게 된다.
    """
    start = script.index("{", pos)
    depth = 0
    for i in range(start, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[start : i + 1]
    raise AssertionError("콜백 본문의 중괄호 짝이 맞지 않는다")


def _array_bindings(script: str, owners: set[str]) -> list[tuple[str, str, str, str]]:
    """``D.recent.map(function (x) {...})`` 를 찾는다. (소유 토큰, 배열 키, 인자명, 본문)."""
    alt = "|".join(re.escape(o) for o in sorted(owners))
    pattern = re.compile(
        r"\b(" + alt + r")\.(" + IDENT + r")\.map\(\s*function\s*\(\s*(" + IDENT + r")"
    )
    return [
        (m.group(1), m.group(2), m.group(3), _callback_body(script, m.end()))
        for m in pattern.finditer(script)
    ]


def _container(payload: dict[str, Any], owner: str, aliases: dict[str, str]) -> dict[str, Any]:
    """배열이 들어 있는 사전. ``D`` 면 payload 자신, 별칭이면 그 하위 블록."""
    if owner == "D":
        return payload
    block = payload[aliases[owner]]
    assert isinstance(block, dict)
    return block


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture
def bars() -> tuple[Bar, ...]:
    """워밍업을 충분히 넘기는 결정론적 봉."""
    return make_series(150, drift=0.0012, amplitude=0.012)


@pytest.fixture
def payload(bars: tuple[Bar, ...], cfg: StrategyConfig) -> dict[str, Any]:
    return build_desk.build_payload(bars, cfg, EWY_VOL, now=FIXED_NOW)


@pytest.fixture
def script() -> str:
    return _logic_script(build_desk.TEMPLATE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 계약
# ---------------------------------------------------------------------------


class TestContractExtraction:
    def test_템플릿에서_계약을_실제로_뽑아낸다(self, script: str) -> None:
        """추출기가 조용히 0개를 뽑으면 아래 계약 테스트가 전부 무의미해진다.

        계약 목록을 적는 대신, 추출기가 일하고 있다는 것만 바닥값으로 고정한다.
        정규식이 깨지면 여기가 먼저 터진다.
        """
        top = _fields_of(script, "D")
        aliases = _aliases(script)
        bindings = _array_bindings(script, {"D", *aliases})

        assert len(top) >= 15, f"최상위 필드를 {len(top)}개밖에 못 찾았다"
        assert len(aliases) >= 2, f"별칭을 {len(aliases)}개밖에 못 찾았다"
        assert len(bindings) >= 4, f"배열 바인딩을 {len(bindings)}개밖에 못 찾았다"
        for owner, key, param, body in bindings:
            assert _fields_of(body, param), f"{owner}.{key} 콜백에서 읽는 필드를 못 찾았다"

    def test_페이지가_읽는_최상위_필드가_전부_들어_있다(
        self, script: str, payload: dict[str, Any]
    ) -> None:
        missing = sorted(_fields_of(script, "D") - set(payload))
        assert not missing, f"payload 에 없는 필드를 페이지가 읽는다: {missing}"

    def test_별칭으로_읽는_하위_필드가_전부_들어_있다(
        self, script: str, payload: dict[str, Any]
    ) -> None:
        """``D.cost`` / ``D.rules`` 하위. 여기가 비면 익절표와 비용 문구가 통째로 깨진다."""
        aliases = _aliases(script)
        for token, key in sorted(aliases.items()):
            assert key in payload, f"별칭 {token} 이 가리키는 {key} 가 payload 에 없다"
            block = payload[key]
            assert isinstance(block, dict), f"payload[{key!r}] 는 사전이어야 한다"
            missing = sorted(_fields_of(script, token) - set(block))
            assert not missing, f"payload[{key!r}] 에 없는 필드를 페이지가 읽는다: {missing}"

    def test_배열_원소가_페이지가_읽는_필드를_전부_가진다(
        self, script: str, payload: dict[str, Any]
    ) -> None:
        aliases = _aliases(script)
        bindings = _array_bindings(script, {"D", *aliases})
        for owner, key, param, body in bindings:
            rows = _container(payload, owner, aliases)[key]
            assert isinstance(rows, list) and rows, f"{key} 는 비지 않은 배열이어야 한다"
            wanted = _fields_of(body, param)
            for row in rows:
                missing = sorted(wanted - set(row))
                assert not missing, f"{key} 원소에 없는 필드를 페이지가 읽는다: {missing}"


class TestTemplateInjection:
    @pytest.mark.parametrize("template", TEMPLATES, ids=TEMPLATE_IDS)
    def test_DATA_자리가_정확히_하나다(self, template: Path) -> None:
        """0개면 데이터가 안 들어가고, 2개면 페이지가 두 번 부풀어 깨진다."""
        assert template.read_text(encoding="utf-8").count("__DATA__") == 1

    @pytest.mark.parametrize("template", TEMPLATES, ids=TEMPLATE_IDS)
    def test_주입한_결과가_JSON_으로_다시_파싱된다(
        self, template: Path, payload: dict[str, Any], tmp_path: Path
    ) -> None:
        """발행 전에 파싱까지 해 본다. 깨진 JSON 은 화면을 통째로 비우고 조용하다."""
        out = build_desk.render(payload, template, tmp_path / "out.html")
        text = out.read_text(encoding="utf-8")

        assert "__DATA__" not in text
        block = PAYLOAD_RE.search(text)
        assert block is not None, "발행 파일에서 payload 블록을 찾지 못했다"
        assert json.loads(block.group(1)) == payload

    def test_NaN_은_주입_단계에서_막힌다(self, payload: dict[str, Any], tmp_path: Path) -> None:
        """파이썬 json 은 NaN 을 그대로 뱉지만 브라우저 JSON.parse 는 거부한다.

        값 하나 때문에 페이지 전체가 백지가 된다. 발행 뒤에는 알아채기 어렵다.
        """
        broken = dict(payload)
        broken["price"] = float("nan")
        with pytest.raises(ValueError):
            build_desk.render(broken, build_desk.TEMPLATE, tmp_path / "nan.html")

    def test_스크립트_종료_태그가_섞이면_막는다(
        self, payload: dict[str, Any], tmp_path: Path
    ) -> None:
        """차단 사유 문자열은 설정에서 흘러들어온다. 그대로 넣으면 페이지가 잘린다."""
        broken = dict(payload)
        broken["blockers"] = ["</script><script>alert(1)</script>"]
        with pytest.raises(SystemExit):
            build_desk.render(broken, build_desk.TEMPLATE, tmp_path / "xss.html")


class TestValueTypes:
    def test_fx_는_숫자다(self, payload: dict[str, Any]) -> None:
        """페이지가 ``D.fx.toLocaleString`` 을 부른다. 문자열이면 그 자리가 비고 만다."""
        assert isinstance(payload["fx"], int | float)
        assert not isinstance(payload["fx"], bool)
        assert payload["fx"] > 0

    def test_nextOpen_은_공백으로_쪼개지는_한국시간_형식이다(self, payload: dict[str, Any]) -> None:
        """페이지가 ``split(" ")[0]`` / ``[1]`` 로 날짜와 시각을 나눠 쓴다.

        기준 시각은 2026-09-19(토) 21:21 KST. 주말이므로 다음 월요일 개장이다.
        """
        value = payload["nextOpen"]
        assert re.fullmatch(r"\d{2}/\d{2}\([월화수목금]\) \d{2}:\d{2}", value)
        assert len(value.split(" ")) == 2
        assert value == "09/21(월) 22:30"

    def test_개장_전이면_오늘을_개장_후면_다음_거래일을_안내한다(self) -> None:
        """09:30 ET 를 기준으로 갈린다. 한국시간으로는 같은 날 저녁 전후다."""
        before = dt.datetime(2026, 9, 21, 21, 0, tzinfo=KST)  # ET 월 08:00, 개장 전
        after = dt.datetime(2026, 9, 21, 23, 0, tzinfo=KST)  # ET 월 10:00, 장중
        assert build_desk.next_open_kst(before) == "09/21(월) 22:30"
        assert build_desk.next_open_kst(after) == "09/22(화) 22:30"

    def test_휴장일을_건너뛴다(self) -> None:
        """2026-09-07 은 노동절이다. 달력 없이 "평일이면 개장" 으로 세다가
        "월요일이면 판가름난다" 고 잘못 안내한 적이 있다.
        """
        friday = dt.datetime(2026, 9, 4, 23, 0, tzinfo=KST)  # ET 금 10:00, 장중
        assert build_desk.next_open_kst(friday) == "09/08(화) 22:30"

    def test_blockers_는_문자열_배열이고_allowed_와_어긋나지_않는다(
        self, payload: dict[str, Any]
    ) -> None:
        names = {c["name"] for c in payload["checks"]}
        assert all(isinstance(b, str) for b in payload["blockers"])
        assert set(payload["blockers"]) <= names
        assert payload["allowed"] is (len(payload["blockers"]) == 0)
        assert payload["allowed"] == all(c["ok"] for c in payload["checks"])

    def test_checks_의_ok_는_진짜_bool_이다(self, payload: dict[str, Any]) -> None:
        """페이지가 삼항 연산자로 통과/차단을 그린다. JSON 에 0/1 이 가면 의미가 뒤집힌다."""
        for check in payload["checks"]:
            assert isinstance(check["ok"], bool)
            assert isinstance(check["name"], str)
            assert isinstance(check["detail"], str)

    def test_decayDaily_는_음수이고_20거래일_복리가_계산된다(self, payload: dict[str, Any]) -> None:
        """페이지가 ``(1+decayDaily)^20`` 을 계산한다. 양수거나 -1 이하면 화면이 거짓말을 한다."""
        decay = payload["decayDaily"]
        assert isinstance(decay, float)
        assert -1.0 < decay < 0.0
        horizon = 1.0 - (1.0 + decay) ** 20
        assert 0.0 < horizon < 1.0


class TestDomainNumbers:
    def test_감쇠는_레버리지_제곱항을_쓴다(self) -> None:
        """``-0.5*(L^2-L)*sigma^2``. L=3 이면 ``-3*sigma^2`` 다.

        이 식을 ``-sigma^2`` 같은 것으로 바꾸면 3배 상품의 진짜 비용이
        3분의 1로 보인다. 보유 기간을 늘리는 판단이 그 숫자 위에서 내려진다.
        """
        assert build_desk.decay_daily(0.04) == pytest.approx(-3.0 * 0.04**2)
        assert build_desk.decay_daily(0.0) == 0.0
        # 변동성이 2배면 감쇠는 4배다. 제곱항이 살아 있는지 보는 불변식이다.
        assert build_desk.decay_daily(0.08) == pytest.approx(4.0 * build_desk.decay_daily(0.04))

    def test_왕복비용에_슬리피지_왕복분이_들어_있다(
        self, payload: dict[str, Any], cfg: StrategyConfig
    ) -> None:
        """``krw_cost`` / ``krw_proceeds`` 는 슬리피지를 한 번도 참조하지 않는다.

        ``round_trip_drag`` 만 그대로 쓰면 0.64% 가 나오지만 실제 왕복은 0.94% 다.
        익절 1단이 +5% 인 전략에서 0.3%p 는 결론을 뒤집는 크기다(AGENTS.md 도메인 함정).
        """
        # payload 는 소수점 5자리로 반올림해 넣는다. 허용 오차는 그 반올림 폭이다.
        round_trip = payload["cost"]["roundTrip"]
        assert round_trip == pytest.approx(
            cfg.cost.round_trip_drag + 2.0 * cfg.cost.slippage_rate, abs=1e-5
        )
        assert round_trip > cfg.cost.round_trip_drag
        assert round_trip == pytest.approx(0.0094, abs=2e-4)

    def test_EWY_를_못_받으면_KORU_변동성의_3분의1로_근사한다(self, bars: tuple[Bar, ...]) -> None:
        """네트워크 실패로 페이지 전체를 못 만드는 것보다 근사값이 낫다.

        KORU 는 EWY 일간수익률의 3배를 추종하므로 ``sigma_EWY ~ sigma_KORU / 3`` 이다.
        """
        sigma = build_desk.fallback_daily_vol(bars)
        assert sigma > 0

        rets = ind.returns([b.close for b in bars[-(build_desk.VOL_WINDOW + 1) :]])
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        assert sigma == pytest.approx(math.sqrt(var) / build_desk.LEVERAGE)

    def test_규칙_수치는_전부_설정에서_온다(
        self, payload: dict[str, Any], cfg: StrategyConfig
    ) -> None:
        """화면에 상수를 박아 두면 설정을 바꿔도 화면만 옛 규칙을 말한다."""
        rules = payload["rules"]
        assert rules["hardStop"] == cfg.hard_stop_krw_return
        assert rules["maxHold"] == cfg.max_holding_days
        assert rules["stopAtr"] == cfg.stop_atr_multiple
        assert rules["maxStopPct"] == cfg.max_stop_pct
        assert rules["capital"] == cfg.capital_krw
        assert rules["maxPos"] == cfg.risk.max_position_krw
        assert [s["k"] for s in rules["ladder"]] == [s.krw_return for s in cfg.take_profit]
        assert [s["f"] for s in rules["ladder"]] == [s.sell_fraction for s in cfg.take_profit]
        assert [s["m"] for s in rules["scaleIn"]] == [s.atr_multiple for s in cfg.scale_in]
        assert [s["w"] for s in rules["scaleIn"]] == [s.weight for s in cfg.scale_in]

    def test_recent_는_마지막_10봉이고_수익률이_직전봉_대비다(
        self, payload: dict[str, Any], bars: tuple[Bar, ...]
    ) -> None:
        rows = payload["recent"]
        assert len(rows) == build_desk.RECENT_BARS
        assert [r["d"] for r in rows] == [b.ts.strftime("%m/%d") for b in bars[-10:]]
        assert rows[-1]["c"] == pytest.approx(bars[-1].close, abs=0.005)
        assert rows[-1]["r"] == pytest.approx(bars[-1].close / bars[-2].close - 1.0, abs=5e-5)

    def test_확정_봉은_마지막_봉이다(self, payload: dict[str, Any], bars: tuple[Bar, ...]) -> None:
        """장중 가격이 아니라 확정 봉이라는 것이 이 페이지의 전제다."""
        assert payload["bar"] == bars[-1].ts.strftime("%Y-%m-%d")
        assert payload["price"] == pytest.approx(bars[-1].close, abs=0.005)
        assert payload["updated"] == "2026-09-19 21:21"


class TestConfigResolution:
    def test_설정이_없는_환경에서는_커밋된_프리셋을_쓴다(self) -> None:
        """``config/strategy.yaml`` 은 .gitignore 에 있다. 저장소를 새로 받은 환경
        (클라우드 루틴 등)에는 그 파일이 없고, 내장 기본값으로 돌아가면 로컬과
        다른 수치가 조용히 나온다. 그래서 대체 프리셋은 반드시 커밋되어 있어야 한다.
        """
        fallback = build_desk.ROOT / "config" / "frequent.example.yaml"
        assert fallback.exists()

        loaded, path = build_desk.resolve_config(str(fallback))
        assert path == fallback
        assert len(loaded.take_profit) == 6

    def test_없는_설정_경로는_조용히_기본값으로_넘어가지_않는다(self, tmp_path: Path) -> None:
        """오타 난 경로에서 기본값으로 돌아가면 의도한 설정으로 돈다고 착각한다."""
        with pytest.raises(SystemExit):
            build_desk.resolve_config(str(tmp_path / "없는설정.yaml"))


def test_두_생성기가_같은_주입_구현을_쓴다() -> None:
    """__DATA__ 주입과 파싱 검증은 한 곳에만 있어야 한다.

    복사해 두면 한쪽만 고쳐지고, 고치지 않은 쪽은 깨진 페이지를 조용히 발행한다.
    """
    assert build_desk.render is build_site.render
    assert build_desk.resolve_config is build_site.resolve_config
    assert build_desk.next_open_kst is build_site.next_open_kst
