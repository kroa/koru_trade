# KORU 자동매매 — 개발 명령 모음
#
# 하네스 규약: 소스를 고쳤으면 `make verify` 가 전부 통과해야 완료다.
# 상세는 AGENTS.md 를 보라.

PYTHON ?= python
VENV_PY := $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts/python.exe,$(if $(wildcard .venv/bin/python),.venv/bin/python,$(PYTHON)))

.DEFAULT_GOAL := help
.PHONY: help setup verify verify-fast fix test lint types secrets gate backtest scan walkforward sweep tick status doctor clean

help:  ## 사용 가능한 명령 목록
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## 가상환경 생성 + 의존성 설치 + git 훅 등록
	$(PYTHON) -m venv .venv --system-site-packages
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -e ".[dev,data]"
	git config core.hooksPath .githooks
	@echo ""
	@echo "설정 완료. 다음: cp .env.example .env 후 값을 채워라"

verify:  ## 전체 하네스 게이트 (커밋 전 필수)
	$(VENV_PY) scripts/verify.py

verify-fast:  ## 빠른 게이트 (타입체크·커버리지 생략)
	$(VENV_PY) scripts/verify.py --fast

fix:  ## 자동 수정 가능한 항목을 고치고 재검증
	$(VENV_PY) scripts/verify.py --fix

test:  ## 테스트만
	$(VENV_PY) -m pytest -q

lint:  ## 린트만
	$(VENV_PY) -m ruff check src tests scripts

types:  ## 타입 검사만
	$(VENV_PY) -m mypy

secrets:  ## 비밀정보 검사만
	$(VENV_PY) -m pytest tests/test_security.py -q --no-cov

gate:  ## 지금 진입해도 되는지 판정
	$(VENV_PY) -m koru_trade gate

backtest:  ## 백테스트
	$(VENV_PY) -m koru_trade backtest --trades

scan:  ## 전방 시뮬레이션 분포
	$(VENV_PY) -m koru_trade scan

walkforward:  ## 워크포워드 + 몬테카를로
	$(VENV_PY) -m koru_trade walkforward

sweep:  ## 파라미터 민감도
	$(VENV_PY) -m koru_trade sweep

tick:  ## 실거래 1틱 (기본 DRY_RUN)
	$(VENV_PY) -m koru_trade tick

status:  ## 현재 포지션/리스크 상태
	$(VENV_PY) -m koru_trade status

doctor:  ## 설정과 자격증명 점검 (값은 마스킹)
	$(VENV_PY) -m koru_trade doctor

clean:  ## 캐시·빌드 산출물 삭제
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml build dist *.egg-info
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
