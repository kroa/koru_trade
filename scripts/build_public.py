"""GitHub Pages 로 내보낼 공개 사이트 한 벌을 만든다.

    python scripts/build_public.py

``docs/`` 아래에 세 파일을 만든다. GitHub Pages 를 "main 브랜치 /docs" 로
설정해 두면 푸시할 때마다 공개 주소가 갱신된다.

    docs/index.html    두 페이지로 가는 표지
    docs/desk.html     상황실
    docs/signals.html  신호 원장

공개 모드로 만드는 이유
-----------------------
페이지에 계좌번호나 토큰은 처음부터 없다. 그런데 ``capital_krw`` 와
``risk.max_position_krw`` 는 **얼마를 굴리는지를 그대로 드러낸다.** 수량·
투입금액·실현손익이 전부 거기서 파생되므로, 공개 배포본은 기준 자본
100만원으로 환산해서 만든다(:func:`_site_common.anonymize`).

비율은 보존된다. 전략의 모든 문턱이 수익률 기준이라 판정과 익절 계단은
똑같이 읽히고, 주수만 실제와 달라진다.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _site_common import PUBLIC_CAPITAL_KRW  # noqa: E402

DOCS = ROOT / "docs"

PAGES = [
    ("build_glance.py", "index.html", "오늘 사야 하나", "판정 한 장. 표지를 겸한다"),
    ("build_desk.py", "desk.html", "상황실", "지금 살 수 있는 상태인지, 규칙이 무엇인지"),
    ("build_site.py", "signals.html", "신호 원장", "3년치 매매 이력과 검증 결과"),
]

MARKER = "<!--MORE-->"
"""판정 페이지 안에서 "다른 장 보기" 줄이 들어갈 자리."""

MORE = """<nav class="more">
  <div class="more-h">더 자세히</div>
{cards}
</nav>
<style>
.more{{margin-top:44px;padding-top:22px;border-top:1px solid var(--rule);
  display:grid;gap:10px}}
.more-h{{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px;
  letter-spacing:.18em;text-transform:uppercase;color:var(--ink-3);margin-bottom:2px}}
.more a{{display:block;border:1px solid var(--rule);padding:15px 17px;
  text-decoration:none;color:var(--ink);transition:border-color .16s}}
.more a:hover{{border-color:var(--ink)}}
.more a:focus-visible{{outline:2px solid var(--stop);outline-offset:2px}}
.more a h2{{margin:0 0 3px;font-family:"Hahmlet",serif;font-weight:600;font-size:16px}}
.more a p{{margin:0;font-size:13.5px;color:var(--ink-2)}}
</style>"""


def run(script: str, out: Path) -> None:
    cmd = [sys.executable, str(ROOT / "scripts" / script), "--public", "--out", str(out)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        raise SystemExit(f"{script} 실패 (exit {proc.returncode})")
    for line in (proc.stdout or "").splitlines():
        print(f"  {line}")


def stamp(path: Path) -> tuple[str, str]:
    """생성된 페이지에서 기준 봉과 갱신 시각을 읽는다."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r'<script id="payload" type="application/json">(.*?)</script>', text, re.S)
    if not m:
        return "?", "?"
    d = json.loads(m.group(1))
    return str(d.get("bar") or d.get("generated") or "?"), str(d.get("updated") or "?")


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="GitHub Pages 공개 사이트 생성").parse_args(argv)
    DOCS.mkdir(exist_ok=True)

    # 지금 공개돼 있는 페이지가 어느 봉 기준인지 먼저 읽어 둔다.
    # 시세 제공자가 어제 봉을 최신으로 내려보내는 일이 실제로 있었다
    # (2026-09-24). 그걸 그대로 발행하면 공개 화면이 하루 뒤로 간다.
    published = None
    if (DOCS / "index.html").exists():
        try:
            published = stamp(DOCS / "index.html")[0]
        except Exception:
            published = None

    cards = []
    bar = updated = "?"
    for script, name, title, desc in PAGES:
        out = DOCS / name
        print(f"[{name}]")
        run(script, out)
        b, u = stamp(out)
        if b != "?":
            bar, updated = b, (u if u != "?" else updated)
        cards.append(f'  <a class="card" href="{name}"><h2>{title}</h2><p>{desc}</p></a>')

    # 표지는 "오늘 사야 하나"(index.html)가 겸한다. 링크만 모아 둔 페이지를 하나
    # 더 두면 "화면이 너무 복잡하다" 는 문제를 한 겹 더 쌓게 된다. 대신 그 페이지
    # 아래에 나머지 두 장으로 가는 줄을 덧붙인다.
    index = DOCS / "index.html"
    html = index.read_text(encoding="utf-8")
    if MARKER not in html:
        raise SystemExit(f"{index.name} 에 {MARKER} 표식이 없다. 템플릿이 바뀌었다")
    index.write_text(
        html.replace(MARKER, MORE.format(cards="\n".join(cards[1:]))), encoding="utf-8"
    )
    print(f"\n표지 겸 판정: {index}  ({index.stat().st_size:,} bytes)")
    print(f"기준 봉 {bar} · 갱신 {updated}")

    # 실제 운용액이 남아 있으면 공개해선 안 된다. 눈으로 확인하지 말고 검사한다.
    #
    # 문자열로 찾으면 안 된다. 유동성 하한이 "$3,000,000"(달러)이라서 원화
    # 자본 3,000,000 과 자릿수가 같다. 통화가 다른 값을 누출로 잡으면 매번
    # 오탐이 나고, 그러면 검사를 꺼 버리게 된다. 페이로드의 자본 필드만 본다.
    checked = 0
    for name in ("desk.html", "signals.html"):
        text = (DOCS / name).read_text(encoding="utf-8")
        m = re.search(r'<script id="payload" type="application/json">(.*?)</script>', text, re.S)
        if m is None:
            raise SystemExit(f"{name}: 페이로드를 찾지 못했다")
        d = json.loads(m.group(1))
        # ``capital`` 만 본다. 한도·수량·투입금액은 전부 여기서 파생되므로
        # 이 값이 기준 자본이면 나머지도 실제 운용액이 아니다. 예컨대
        # ``maxPos`` 는 자본의 1.67배가 정상 비율이지 누출이 아니다.
        for path in (("rules", "capital"), ("summary", "capital")):
            node: object = d
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    node = None
                    break
                node = node[key]
            if node is None:
                continue
            checked += 1
            if float(node) != PUBLIC_CAPITAL_KRW:  # type: ignore[arg-type]
                raise SystemExit(
                    f"{name}: {'.'.join(path)} = {float(node):,.0f} 원. "
                    f"공개 기준 자본 {PUBLIC_CAPITAL_KRW:,.0f}원이어야 한다 — "
                    "--public 없이 만들었거나 anonymize 가 깨졌다"
                )
    if checked == 0:
        raise SystemExit("자본 필드를 하나도 찾지 못했다. 검사가 망가졌다")

    # 시세 후퇴 차단. 날짜 문자열은 YYYY-MM-DD 라 사전순 비교가 곧 시간순이다.
    if published and published != "?" and bar != "?" and bar < published:
        raise SystemExit(
            f"기준 봉이 뒤로 갔다: 새로 만든 것 {bar} < 이미 공개된 것 {published}. "
            "시세 제공자가 과거 봉을 최신으로 주고 있다. 발행하지 않는다"
        )
    print(f"확인: 자본 필드 {checked}개 모두 공개 기준 이하")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
