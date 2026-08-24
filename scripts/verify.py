#!/usr/bin/env python3
"""하네스 검증 게이트 — 이 스크립트 하나가 "완료" 의 정의다.

소스를 고쳤으면 이 스크립트가 **전부 통과**해야 작업이 끝난 것이다.
사람이든 AI 에이전트든 예외 없다. 통과하지 않은 변경은 커밋되지 않는다
(`.githooks/pre-commit` 이 이 스크립트를 부른다).

게이트 순서
-----------
빠르고 값싼 것부터 돌려서 실패를 일찍 만난다.

1. **secrets**  — 비밀정보·개인정보가 커밋 대상에 섞였는지 (가장 먼저, 가장 중요)
2. **format**   — ruff format 규칙 준수
3. **lint**     — ruff check
4. **types**    — mypy strict
5. **tests**    — pytest + 커버리지 임계값

사용법::

    python scripts/verify.py            # 전부 실행
    python scripts/verify.py --fast     # 타입체크·커버리지 생략(개발 중 빠른 확인용)
    python scripts/verify.py --fix      # 자동 수정 가능한 것은 고치고 진행
    python scripts/verify.py lint tests # 특정 게이트만
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable


@dataclass(frozen=True)
class Gate:
    """검증 게이트 하나."""

    name: str
    description: str
    command: list[str]
    fix_command: list[str] | None = None
    fast: bool = True
    """--fast 모드에서도 실행할지."""


GATES: tuple[Gate, ...] = (
    Gate(
        name="secrets",
        description="비밀정보·개인정보 유출 검사",
        command=[PY, "-m", "pytest", "tests/test_security.py", "-q", "--no-cov", "-x"],
    ),
    Gate(
        name="format",
        description="코드 포맷 검사",
        command=[PY, "-m", "ruff", "format", "--check", "src", "tests", "scripts"],
        fix_command=[PY, "-m", "ruff", "format", "src", "tests", "scripts"],
    ),
    Gate(
        name="lint",
        description="정적 분석(ruff)",
        command=[PY, "-m", "ruff", "check", "src", "tests", "scripts"],
        fix_command=[PY, "-m", "ruff", "check", "--fix", "src", "tests", "scripts"],
    ),
    Gate(
        name="types",
        description="타입 검사(mypy strict)",
        command=[PY, "-m", "mypy"],
        fast=False,
    ),
    Gate(
        name="tests",
        description="테스트 + 커버리지 임계값",
        command=[PY, "-m", "pytest", "-q"],
        fast=False,
    ),
    Gate(
        name="tests-fast",
        description="테스트(커버리지 없이)",
        command=[PY, "-m", "pytest", "-q", "--no-cov", "-x"],
    ),
)

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _supports_color() -> bool:
    """ANSI 색상을 쓸 수 있는 터미널인지."""
    if sys.platform != "win32":
        return sys.stdout.isatty()
    # Windows Terminal / Git Bash 는 지원한다. 레거시 cmd.exe 는 안전하게 끈다.
    return sys.stdout.isatty() and shutil.which("tput") is not None


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if _supports_color() else text


def run_gate(gate: Gate, *, fix: bool) -> tuple[bool, float]:
    """게이트 하나를 실행한다.

    Returns:
        (통과 여부, 소요 시간(초))
    """
    if fix and gate.fix_command:
        subprocess.run(gate.fix_command, cwd=REPO, check=False)

    print(f"  {_c('▶', DIM)} {gate.name:<12} {_c(gate.description, DIM)}")
    started = time.monotonic()
    result = subprocess.run(gate.command, cwd=REPO, check=False)
    elapsed = time.monotonic() - started
    ok = result.returncode == 0

    mark = _c("통과", GREEN) if ok else _c("실패", RED)
    print(f"  {mark} {gate.name:<12} {_c(f'{elapsed:.1f}초', DIM)}\n")
    return ok, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="하네스 검증 게이트. 전부 통과해야 작업이 완료된 것이다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "gates",
        nargs="*",
        help=f"실행할 게이트 (기본: 전부). 선택지: {', '.join(g.name for g in GATES)}",
    )
    parser.add_argument("--fast", action="store_true", help="타입체크·커버리지 생략")
    parser.add_argument("--fix", action="store_true", help="자동 수정 가능한 것은 고친다")
    args = parser.parse_args(argv)

    if args.gates:
        unknown = set(args.gates) - {g.name for g in GATES}
        if unknown:
            print(_c(f"알 수 없는 게이트: {sorted(unknown)}", RED))
            return 2
        selected = [g for g in GATES if g.name in args.gates]
    elif args.fast:
        selected = [g for g in GATES if g.fast]
    else:
        selected = [g for g in GATES if g.name != "tests-fast"]

    print()
    print(_c("=" * 66, BOLD))
    mode = " (fast)" if args.fast else ""
    print(_c(f"  KORU 하네스 검증{mode} — {len(selected)}개 게이트", BOLD))
    print(_c("=" * 66, BOLD))
    print()

    failures: list[str] = []
    total = 0.0
    for gate in selected:
        ok, elapsed = run_gate(gate, fix=args.fix)
        total += elapsed
        if not ok:
            failures.append(gate.name)

    print(_c("=" * 66, BOLD))
    if failures:
        print(_c(f"  실패: {', '.join(failures)}  ({total:.1f}초)", RED))
        print(_c("=" * 66, BOLD))
        print()
        print("  이 변경은 아직 완료된 것이 아니다. 위 오류를 고치고 다시 실행하라.")
        print(f"  자동 수정 가능한 항목: {_c('python scripts/verify.py --fix', YELLOW)}")
        print()
        return 1

    print(_c(f"  전체 통과 ({total:.1f}초)", GREEN))
    print(_c("=" * 66, BOLD))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
