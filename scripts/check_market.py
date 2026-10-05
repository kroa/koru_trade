"""KORU 미국 본장 개장 전(오후/밤) 시황 및 진입 점검 CLI.

미국 본장 개장 전(예: 21:40 KST, 서머타임 기준 22:30 개장)에 실행하여
프리마켓 시세, 환율, 당일 시스템 진입 신호, 박스권 위치를 점검한다.

    python scripts/check_market.py
    python scripts/check_market.py --notify     # 텔레그램 전송
    python scripts/check_market.py --ai         # Google Gemini 브리핑 강제 실행

작업 스케줄러 등록(매일 21:40 KST):
    schtasks /create /tn "KORU pre-market" /tr "C:\\Koru_Trade\\scripts\\check_market.bat" /sc daily /st 21:40 /f
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from _site_common import (  # noqa: E402
    KST,
    SITE_PERIOD,
    confirmed_bars,
    resolve_config,
    verdict_schedule,
)

from koru_trade.console import use_utf8_console  # noqa: E402
from koru_trade.data import load_bars  # noqa: E402
from koru_trade.models import Bar  # noqa: E402
from koru_trade.notify.telegram import TelegramNotifier  # noqa: E402
from koru_trade.strategy import entry_limit_price, evaluate_entry  # noqa: E402


@dataclass(frozen=True)
class BoxRange:
    """박스권 밴드 정보."""

    low_20d: float
    high_20d: float
    pct_20d: float
    low_60d: float
    high_60d: float
    pct_60d: float
    zone: str  # "상단" / "중심" / "하단"
    advice: str


def compute_box_range(bars: Sequence[Bar], current_price: float) -> BoxRange:
    """최근 20거래일(단기) 및 60거래일(중기) 기준 박스권 위치를 계산한다."""
    sample_20 = bars[-20:] if len(bars) >= 20 else bars
    sample_60 = bars[-60:] if len(bars) >= 60 else bars

    low_20 = min(b.low for b in sample_20)
    high_20 = max(b.high for b in sample_20)
    span_20 = high_20 - low_20
    pct_20 = max(0.0, min(1.0, (current_price - low_20) / span_20)) if span_20 > 0 else 0.5

    low_60 = min(b.low for b in sample_60)
    high_60 = max(b.high for b in sample_60)
    span_60 = high_60 - low_60
    pct_60 = max(0.0, min(1.0, (current_price - low_60) / span_60)) if span_60 > 0 else 0.5

    # 실전 단기 박스권(20일 기준: 최근 9월~10월) 중심 판정
    if pct_20 >= 0.70:
        zone = "상단"
        advice = (
            "⚠️ [단기 박스권 상단 저항선 부근 ($23~$24 저항)]\n"
            "   시스템 초록불(매수 허용)이라도 전량 몰빵 매수는 고점 추격매수 위험이 큽니다.\n"
            "   1차 분할 매수(30~40%)만 제한적으로 진입하거나, $23.5 돌파 안착 또는 $21대 눌림목을 대기하세요."
        )
    elif pct_20 <= 0.30:
        zone = "하단"
        advice = (
            "✅ [단기 박스권 하단 지지선 부근 ($19~$20 지지)]\n"
            "   지지선 근처로 손익비가 가장 우수한 구간입니다.\n"
            "   시스템 신호 부합 시 적극적인 분할 매수(Scale-in)가 유리합니다."
        )
    else:
        zone = "중심"
        advice = (
            "⚖️ [단기 박스권 중심 구간 ($21~$22.5)]\n"
            "   이동평균선(EMA10/EMA30) 지지력을 확인하며 시스템 기준 비중으로 분할 진입이 적절합니다."
        )

    return BoxRange(
        low_20d=round(low_20, 2),
        high_20d=round(high_20, 2),
        pct_20d=round(pct_20, 4),
        low_60d=round(low_60, 2),
        high_60d=round(high_60, 2),
        pct_60d=round(pct_60, 4),
        zone=zone,
        advice=advice,
    )


def fetch_live_price(fallback_price: float) -> tuple[float, str]:
    """프리마켓 또는 최근 체결가를 조회한다."""
    try:
        import yfinance as yf

        ticker = yf.Ticker("KORU")
        fast = ticker.fast_info
        last_price = getattr(fast, "last_price", None)
        if last_price is not None and last_price > 0:
            return round(float(last_price), 2), "live"
    except Exception:
        pass
    return fallback_price, "close_fallback"


def call_gemini_briefing(prompt_text: str, api_key: str) -> str | None:
    """Google AI Studio (Gemini Flash) REST API를 호출하여 브리핑 코멘트를 생성한다.

    금융 계좌, 잔고, 개인 식별 정보는 절대로 전송하지 않으며,
    정제된 시세 지표 요약 텍스트만 전송한다.
    """
    model_name = "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "당신은 미국 3배 레버리지 ETF(KORU) 퀀트 트레이딩 시스템의 장전 어시스턴트입니다.\n"
                            "아래 제공된 정량 지표와 박스권 진단 결과를 바탕으로, 트레이더가 오늘 밤 미국장 개장 시 "
                            "참고할 핵심 브리핑을 3~4줄로 명확하고 간결하게 작성해주세요.\n\n"
                            f"{prompt_text}"
                        )
                    }
                ]
            }
        ]
    }

    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return str(parts[0].get("text", "")).strip()
    except Exception as exc:
        print(f"  [경고] Gemini API 호출 실패: {exc}")
        return None
    return None


def format_report(
    when: dict[str, Any],
    last_bar: Bar,
    cur_price: float,
    price_source: str,
    limit_price: float,
    allowed: bool,
    blockers: list[str],
    box: BoxRange,
    gemini_summary: str | None,
) -> str:
    """터미널 및 텔레그램용 리포트 본문을 조립한다."""
    status_icon = "🟢 [매수 허용]" if allowed else "🔴 [진입 차단]"
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 KORU 미국 본장 개장 전 체크 ({dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')})",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"⏰ 오늘 밤 개장: {when['open']} (마감 {when['close']})",
        f"📈 확정 종가: ${last_bar.close:.2f} ({last_bar.ts.strftime('%Y-%m-%d')})",
        f"💲 현재/프리마켓: ${cur_price:.2f} ({price_source})",
        f"🎯 진입 지정가: ${limit_price:.2f} (확정 종가 +15bp)",
        "────────────────────────────────────────",
        f"🚦 시스템 판정: {status_icon}",
    ]
    if blockers:
        lines.append(f"   차단 사유: {', '.join(blockers)}")

    lines.extend(
        [
            "────────────────────────────────────────",
            f"📦 20일 단기 박스권 진단: {box.zone} 구간 ({box.pct_20d * 100:.1f}%)",
            f"   단기 밴드 (20일): ${box.low_20d:.2f} ~ ${box.high_20d:.2f}",
            f"   중기 밴드 (60일): ${box.low_60d:.2f} ~ ${box.high_60d:.2f} ({box.pct_60d * 100:.1f}%)",
            f"{box.advice}",
            "────────────────────────────────────────",
        ]
    )

    if gemini_summary:
        lines.extend(
            [
                "🤖 Google Gemini AI 마켓 인사이트:",
                gemini_summary,
                "────────────────────────────────────────",
            ]
        )

    return "\n".join(lines)


def run_check(notify: bool = False, force_ai: bool = False) -> int:
    """장전 체크 실행 본체."""
    use_utf8_console()
    print("[*] KORU 장전 시황 및 진입 점검 시작...")

    cfg, _ = resolve_config(None)
    bars_koru = confirmed_bars(load_bars("KORU", period=SITE_PERIOD))

    if len(bars_koru) < 35:
        print("[오류] 분석에 필요한 KORU 봉 데이터가 부족합니다.")
        return 1

    last_bar = bars_koru[-1]
    when = verdict_schedule(last_bar.ts.date())

    # 1. 진입 게이트 판정
    signal = evaluate_entry(bars_koru, cfg)
    allowed = signal.allowed
    blockers = [c.name for c in signal.checks if not c.passed]
    limit_px = entry_limit_price(last_bar.close, cfg)

    # 2. 현재 시세 및 박스권 계산
    cur_price, price_source = fetch_live_price(last_bar.close)
    box = compute_box_range(bars_koru, cur_price)

    # 3. Google Gemini 브리핑 (API 키 존재 시)
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    gemini_summary: str | None = None
    if gemini_key or force_ai:
        if gemini_key:
            prompt = (
                f"- 종목: KORU (MSCI 한국 3배 레버리지 ETF)\n"
                f"- 확정 종가: ${last_bar.close:.2f}, 현재가: ${cur_price:.2f}\n"
                f"- 시스템 상태: {'초록불(진입 허용)' if allowed else '빨간불(차단)'}\n"
                f"- 기준 지정가: ${limit_px:.2f}\n"
                f"- 20일 단기 박스권: ${box.low_20d:.2f} ~ ${box.high_20d:.2f} (현재 {box.zone} {box.pct_20d * 100:.1f}% 지점)\n"
                f"- 차단 사유: {', '.join(blockers) if blockers else '없음 (모든 게이트 통과)'}"
            )
            gemini_summary = call_gemini_briefing(prompt, gemini_key)
        else:
            print("  [알림] GEMINI_API_KEY가 없어 AI 브리핑을 건너뜁니다.")

    # 4. 리포트 생성 및 터미널 출력
    report = format_report(
        when=when,
        last_bar=last_bar,
        cur_price=cur_price,
        price_source=price_source,
        limit_price=limit_px,
        allowed=allowed,
        blockers=blockers,
        box=box,
        gemini_summary=gemini_summary,
    )
    print(report)

    # 5. 텔레그램 발송 (요청 시)
    if notify:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            try:
                notifier = TelegramNotifier(token=token, chat_id=chat_id)
                notifier.send(report)
                print("[+] 텔레그램 브리핑 전송 완료.")
            except Exception as exc:
                print(f"[!] 텔레그램 전송 실패: {exc}")
        else:
            print("[-] TELEGRAM_BOT_TOKEN 또는 CHAT_ID가 설정되지 않아 전송을 건너뜁니다.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="KORU 본장 개장 전 시황 및 진입 점검")
    parser.add_argument("--notify", action="store_true", help="텔레그램 알림 발송")
    parser.add_argument("--ai", action="store_true", help="Gemini AI 브리핑 강제 실행")
    args = parser.parse_args()

    sys.exit(run_check(notify=args.notify, force_ai=args.ai))


if __name__ == "__main__":
    main()
