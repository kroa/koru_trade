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
    ("build_desk.py", "desk.html", "상황실", "지금 살 수 있는 상태인지, 규칙이 무엇인지"),
    ("build_site.py", "signals.html", "신호 원장", "3년치 매매 이력과 검증 결과"),
]

INDEX = """<!doctype html>
<html lang="ko">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KORU</title>
<link rel="icon" href="data:,">
<style>
:root {{
  --ground:#F6F7F9; --panel:#FFFFFF; --line:#DDE1E9;
  --ink-1:#12151C; --ink-2:#4A5265; --ink-3:#79809A; --gold:#8A6420;
  --f-body:"IBM Plex Sans KR",-apple-system,"Malgun Gothic",sans-serif;
  --f-mono:ui-monospace,"Cascadia Mono",monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#0C0E14; --panel:#131722; --line:#262C3B;
    --ink-1:#EEF1F6; --ink-2:#A2AABC; --ink-3:#6E7688; --gold:#D4A24C;
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--ground); color:var(--ink-1);
  font-family:var(--f-body); font-size:15px; line-height:1.6;
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:720px; margin:0 auto; padding:64px 24px 80px; }}
.eyebrow {{ font-family:var(--f-mono); font-size:11px; letter-spacing:.22em;
  text-transform:uppercase; color:var(--gold); margin-bottom:14px; }}
h1 {{ font-size:clamp(30px,6vw,44px); line-height:1.1; letter-spacing:-.02em;
  margin:0 0 14px; font-weight:600; }}
.lede {{ color:var(--ink-2); margin:0 0 40px; max-width:56ch; }}
a.card {{ display:block; background:var(--panel); border:1px solid var(--line);
  border-radius:3px; padding:22px; margin-bottom:14px; text-decoration:none;
  color:inherit; transition:border-color .16s; }}
a.card:hover {{ border-color:var(--ink-3); }}
a.card h2 {{ margin:0 0 5px; font-size:17px; font-weight:600; }}
a.card p {{ margin:0; color:var(--ink-2); font-size:14px; }}
.meta {{ font-family:var(--f-mono); font-size:11.5px; color:var(--ink-3);
  margin-top:36px; padding-top:20px; border-top:1px solid var(--line);
  line-height:1.9; }}
.meta b {{ color:var(--ink-2); font-weight:500; }}
</style>
<div class="wrap">
  <div class="eyebrow">Direxion Daily MSCI South Korea Bull 3X</div>
  <h1>KORU</h1>
  <p class="lede">
    3배 레버리지 ETF 를 규칙으로만 매매하는 시스템의 현재 상태와 과거 기록.
    사람이 그때그때 판단하지 않도록, 살 자격과 팔 이유를 미리 정해 두고 그대로 따른다.
  </p>
{cards}
  <div class="meta">
    <b>기준 봉</b> {bar} &nbsp;·&nbsp; <b>갱신</b> {updated} (한국시간)<br>
    <b>기준 자본</b> 100만원 환산 &nbsp;·&nbsp; 실제 운용액이 아니다<br>
    <b>주의</b> 투자 권유가 아니다. 3배 상품은 방향과 무관하게 매일 녹는다.
  </div>
</div>
"""


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

    index = DOCS / "index.html"
    index.write_text(
        INDEX.format(cards="\n".join(cards), bar=bar, updated=updated), encoding="utf-8"
    )
    print(f"\n표지: {index}  ({index.stat().st_size:,} bytes)")
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
    print(f"확인: 자본 필드 {checked}개 모두 공개 기준 이하")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
