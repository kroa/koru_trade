"""저장소 위생 검사 — GitHub 공개 전 마지막 방어선.

사용자 요구사항: "github에 소스를 올릴건데 개인정보나 민감정보는 올리지 않게 해줘"

이 파일의 테스트는 **커밋되는 모든 파일**을 실제로 읽어서 검사한다.
문서에 "조심하자" 라고 적어두는 것으로는 아무것도 막지 못한다.
검사가 실패하면 하네스가 커밋을 막는다.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# 검사에서 제외할 경로 (생성물·가상환경·캐시)
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "htmlcov",
    "state",
    "logs",
    "reports",
    "node_modules",
    "build",
    "dist",
}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
    ".txt",
    ".sh",
    ".ps1",
    ".env",
    ".example",
    ".csv",
    "",
}

# 값이 명백히 예시임을 나타내는 표식. 이게 들어 있으면 오탐으로 본다.
PLACEHOLDER_MARKERS = (
    "FAKE",
    "EXAMPLE",
    "SAMPLE",
    "DUMMY",
    "PLACEHOLDER",
    "YOUR_",
    "XXXX",
    "0000000000",
    "여기에",
    "TODO",
)


def _iter_repo_files() -> list[Path]:
    """검사 대상 파일 목록.

    ``--cached --others --exclude-standard`` 로 **추적 중인 파일과 아직 git 에 추가하지
    않은 파일을 모두** 가져오고, ``.gitignore`` 대상은 제외한다.

    ``git ls-files`` 만 쓰면 추적되지 않은 새 파일을 놓친다. 자격증명을 새 파일에
    적어 넣은 직후가 가장 위험한 시점인데, 하필 그때 검사가 통과해 버린다.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        listed = {REPO / line for line in out.stdout.splitlines() if line.strip()}
        if listed:
            return sorted(p for p in listed if p.is_file())
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    files: list[Path] = []
    for p in REPO.rglob("*"):
        if not p.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in p.parts):
            continue
        files.append(p)
    return files


def _readable(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    if path.stat().st_size > 2_000_000:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _looks_like_placeholder(line: str) -> bool:
    """이 줄이 실제 값이 아니라 예시/자리표시자인지 판정한다.

    두 가지를 본다.

    1. 명시적 표식(FAKE, EXAMPLE, YOUR_ 등)이 들어 있는가
    2. 같은 문자만 반복되는 토큰이 있는가 (``00000000``, ``XXXXXX``, ``KKKK...``)
       — 사람이 자리표시자로 쓰는 가장 흔한 형태다.
    """
    upper = line.upper()
    if any(marker in upper for marker in PLACEHOLDER_MARKERS):
        return True
    for token in re.split(r"[\s=:,'\"\[\]{}()]+", line):
        stripped = token.lstrip("PS")  # KIS 앱키 접두어는 떼고 본다
        if len(stripped) >= 6 and len(set(stripped)) == 1:
            return True
    return False


@pytest.fixture(scope="module")
def repo_files() -> list[Path]:
    return _iter_repo_files()


class TestGitignore:
    """민감 파일이 애초에 git 에 들어가지 못하게 막혀 있는지."""

    @pytest.fixture
    def gitignore(self) -> str:
        path = REPO / ".gitignore"
        assert path.exists(), ".gitignore 가 없다. 공개 저장소에 필수다"
        return path.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "pattern",
        [
            ".env",
            "state/",
            "*.sqlite",
            "*.db",
            "logs/",
            "*.log",
            ".cache/",
            "*.key",
            "*.pem",
        ],
    )
    def test_필수_무시_패턴이_있다(self, gitignore: str, pattern: str) -> None:
        assert pattern in gitignore, f".gitignore 에 {pattern} 이(가) 없다"

    def test_env_example은_예외로_허용된다(self, gitignore: str) -> None:
        """설정 방법을 알려주려면 예시 파일은 커밋되어야 한다."""
        assert "!.env.example" in gitignore


class TestNoSecretsInRepo:
    """커밋 대상 파일에 실제 비밀정보가 없는지."""

    def test_env_파일이_추적되지_않는다(self, repo_files: list[Path]) -> None:
        tracked = {p.name for p in repo_files}
        assert ".env" not in tracked, ".env 가 git 에 추적되고 있다. 즉시 제거하라"

    def test_상태DB와_로그가_추적되지_않는다(self, repo_files: list[Path]) -> None:
        bad = [
            p
            for p in repo_files
            if p.suffix in (".db", ".sqlite", ".sqlite3", ".log")
            or "state" in p.parts
            or "logs" in p.parts
        ]
        assert not bad, f"체결 이력이 담길 수 있는 파일이 추적되고 있다: {bad}"

    def test_KIS_앱키_형식의_문자열이_없다(self, repo_files: list[Path]) -> None:
        """KIS 앱키는 'PS' + 대문자/숫자 34자 형태다."""
        pattern = re.compile(r"\bPS[A-Z0-9]{34}\b")
        hits: list[str] = []
        for path in repo_files:
            text = _readable(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and not _looks_like_placeholder(line):
                    hits.append(f"{path.relative_to(REPO)}:{lineno}")
        assert not hits, f"KIS 앱키로 보이는 문자열이 있다: {hits}"

    def test_키_이름에_붙은_긴_리터럴이_없다(self, repo_files: list[Path]) -> None:
        """변수명이 key/secret/token/password 인데 긴 문자열이 붙어 있으면 유출이다.

        형식(길이·대소문자)에만 의존하는 탐지는 규격이 조금 달라지면 놓친다.
        **변수 이름**을 함께 보면 훨씬 잘 잡힌다.
        """
        pattern = re.compile(
            r"""(?ix)
            \b\w*(?:app_?key|app_?secret|api_?key|secret_?key|access_?token
                    |auth_?token|password|passwd)\w*
            \s*[:=]\s*
            ['\"]([A-Za-z0-9+/=_-]{20,})['\"]
            """
        )
        hits: list[str] = []
        for path in repo_files:
            if path.suffix not in (".py", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini"):
                continue
            text = _readable(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and not _looks_like_placeholder(line):
                    hits.append(f"{path.relative_to(REPO)}:{lineno}")
        assert not hits, f"자격증명 변수에 실제 값이 박혀 있다: {hits}"

    def test_긴_시크릿_형식_문자열이_없다(self, repo_files: list[Path]) -> None:
        """KIS 앱시크릿은 대문자/숫자 100자 이상이다."""
        pattern = re.compile(r"\b[A-Za-z0-9+/=]{100,}\b")
        hits: list[str] = []
        for path in repo_files:
            if path.suffix in (".csv", ".lock"):
                continue
            text = _readable(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and not _looks_like_placeholder(line):
                    hits.append(f"{path.relative_to(REPO)}:{lineno}")
        assert not hits, f"시크릿으로 보이는 긴 문자열이 있다: {hits}"

    def test_계좌번호_대입문이_없다(self, repo_files: list[Path]) -> None:
        """CANO/account_no 에 숫자 리터럴을 직접 넣은 곳이 없어야 한다."""
        pattern = re.compile(
            r"(?:CANO|account_no|ACNT_PRDT_CD|계좌번호)\s*[:=]\s*['\"]?\d{6,}",
            re.IGNORECASE,
        )
        hits: list[str] = []
        for path in repo_files:
            text = _readable(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and not _looks_like_placeholder(line):
                    hits.append(f"{path.relative_to(REPO)}:{lineno}  {line.strip()[:80]}")
        assert not hits, f"계좌번호로 보이는 값이 직접 적혀 있다: {hits}"

    def test_소스에_이메일_주소가_없다(self, repo_files: list[Path]) -> None:
        """개인 이메일이 소스에 박히지 않도록. 예시 도메인은 허용한다."""
        pattern = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
        allowed_domains = ("example.com", "example.org", "noreply.github.com")
        hits: list[str] = []
        for path in repo_files:
            if path.suffix not in (".py", ".toml", ".yaml", ".yml", ".md", ".cfg"):
                continue
            text = _readable(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for match in pattern.finditer(line):
                    if any(match.group().endswith(d) for d in allowed_domains):
                        continue
                    hits.append(f"{path.relative_to(REPO)}:{lineno}  {match.group()}")
        assert not hits, f"이메일 주소가 소스에 있다: {hits}"

    def test_텔레그램_봇_토큰이_없다(self, repo_files: list[Path]) -> None:
        """텔레그램 봇 토큰은 `숫자ID:35자` 형태다.

        이 토큰 하나면 누구나 그 봇으로 메시지를 보내고 받을 수 있다.
        계좌 자격증명과 같은 등급으로 취급한다.
        """
        pattern = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")
        hits: list[str] = []
        for path in repo_files:
            text = _readable(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and not _looks_like_placeholder(line):
                    hits.append(f"{path.relative_to(REPO)}:{lineno}")
        assert not hits, f"텔레그램 봇 토큰으로 보이는 문자열이 있다: {hits}"

    def test_bearer_토큰_리터럴이_없다(self, repo_files: list[Path]) -> None:
        pattern = re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}")
        hits: list[str] = []
        for path in repo_files:
            text = _readable(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and not _looks_like_placeholder(line):
                    hits.append(f"{path.relative_to(REPO)}:{lineno}")
        assert not hits, f"Bearer 토큰 리터럴이 있다: {hits}"


class TestEnvExample:
    """예시 파일에 실제 값이 들어가지 않았는지."""

    @pytest.fixture
    def env_example(self) -> str:
        path = REPO / ".env.example"
        assert path.exists(), ".env.example 이 없다"
        return path.read_text(encoding="utf-8")

    def test_필수_항목이_모두_설명되어_있다(self, env_example: str) -> None:
        for key in (
            "KIS_APP_KEY",
            "KIS_APP_SECRET",
            "KIS_ACCOUNT_NO",
            "KIS_ACCOUNT_PRODUCT_CODE",
            "KIS_ENV",
            "KORU_DRY_RUN",
        ):
            assert key in env_example, f".env.example 에 {key} 설명이 없다"

    def test_모든_값이_자리표시자다(self, env_example: str) -> None:
        for line in env_example.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if not value:
                continue
            assert (
                set(value) <= set("0123456789")
                or _looks_like_placeholder(value)
                or value in ("paper", "live", "true", "false", "INFO", "DEBUG")
            ), f".env.example 의 {key} 에 실제 값처럼 보이는 것이 들어 있다: {value[:20]}"


class TestRuntimeSafety:
    """실행 시점의 안전 기본값."""

    def test_DRY_RUN의_기본값이_True다(self) -> None:
        """환경변수가 없거나 이상하면 주문이 나가지 않아야 한다."""
        from koru_trade.config import is_dry_run

        assert is_dry_run({}) is True
        assert is_dry_run({"KORU_DRY_RUN": "이상한값"}) is True
        assert is_dry_run({"KORU_DRY_RUN": "true"}) is True
        assert is_dry_run({"KORU_DRY_RUN": "false"}) is False
        assert is_dry_run({"KORU_DRY_RUN": "0"}) is False

    def test_기본_환경이_모의투자다(self) -> None:
        from koru_trade.config import load_credentials

        cred = load_credentials(
            {
                "KIS_APP_KEY": "K" * 36,
                "KIS_APP_SECRET": "S" * 180,
                "KIS_ACCOUNT_NO": "12345678",
                "KIS_ACCOUNT_PRODUCT_CODE": "01",
            }
        )
        assert not cred.is_live
        assert "openapivts" in cred.base_url

    def test_잘못된_환경값은_거부된다(self) -> None:
        from koru_trade.config import load_credentials

        with pytest.raises(ValueError, match=r"paper.*live"):
            load_credentials({"KIS_ENV": "production"})

    def test_전략설정에는_자격증명_필드가_없다(self) -> None:
        """설정 YAML 은 커밋되므로 키가 들어갈 자리가 아예 없어야 한다."""
        from dataclasses import fields

        from koru_trade.config import StrategyConfig

        names = {f.name for f in fields(StrategyConfig)}
        forbidden = {"app_key", "app_secret", "account_no", "token", "password", "secret"}
        assert not (names & forbidden)

    def test_직렬화_결과에_비밀정보가_없다(self) -> None:
        from koru_trade.config import StrategyConfig

        text = str(StrategyConfig().to_dict())
        for word in ("app_key", "appsecret", "account", "token", "password"):
            assert word not in text.lower()

    def test_텔레그램_설정이_repr에_노출되지_않는다(self) -> None:
        from koru_trade.notify import TelegramConfig

        token = "1234567890:FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAK"
        chat = "987654321"
        conf = TelegramConfig(bot_token=token, chat_id=chat)
        for text in (repr(conf), str(conf), f"{conf}"):
            assert token not in text
            assert chat not in text

    def test_알림이_없어도_매매는_계속된다(self) -> None:
        """알림 설정이 없다고 봇이 죽으면 안 된다."""
        from koru_trade.notify import load_telegram_config

        assert load_telegram_config({}) is None
        assert load_telegram_config({"TELEGRAM_BOT_TOKEN": "x"}) is None

    def test_마스킹_함수가_원문을_숨긴다(self) -> None:
        from koru_trade.config import mask_secret

        secret = "PSFAKE567890ABCDEFGHIJKLMNOPQRSTUVWX"  # 자리표시자
        masked = mask_secret(secret)
        assert secret not in masked
        assert masked.startswith("PSFA")
        assert mask_secret(None) == "<미설정>"
        assert mask_secret("ab") == "**"


class TestScannerConfigIntegrity:
    """검사 설정 파일 자체가 멀쩡한지.

    깨진 gitleaks 설정은 오류를 내지 않고 **조용히 아무것도 검사하지 않는다.**
    보호받고 있다고 착각하게 만드는 것이 가장 위험한 상태다.
    실제로 이 저장소의 규칙 두 개가 정규식 안의 단어 경계(\b)가
    백스페이스 문자(0x08)로 들어가 무력화돼 있었다.
    """

    @pytest.fixture
    def gitleaks(self) -> dict:
        import tomllib

        path = REPO / ".gitleaks.toml"
        assert path.exists(), ".gitleaks.toml 이 없다"
        with path.open("rb") as fh:
            return tomllib.load(fh)

    def test_TOML로_파싱된다(self, gitleaks: dict) -> None:
        assert "rules" in gitleaks

    def test_필수_규칙이_있다(self, gitleaks: dict) -> None:
        ids = {r["id"] for r in gitleaks["rules"]}
        assert {"kis-app-key", "kis-app-secret", "kis-account-no", "telegram-bot-token"} <= ids

    def test_모든_정규식이_컴파일된다(self, gitleaks: dict) -> None:
        for rule in gitleaks["rules"]:
            re.compile(rule["regex"])

    def test_제어문자가_섞이지_않았다(self) -> None:
        """0x08 같은 제어문자가 들어가면 정규식이 조용히 죽는다."""
        for name in (".gitleaks.toml", ".env.example", ".gitignore"):
            raw = (REPO / name).read_bytes()
            bad = [b for b in set(raw) if b < 9 or 11 <= b <= 12 or 14 <= b <= 31]
            assert not bad, f"{name} 에 제어문자가 있다: {[hex(b) for b in bad]}"

    def test_규칙이_실제_형식을_잡아낸다(self, gitleaks: dict) -> None:
        """규칙이 있다고 동작하는 것은 아니다. 표본으로 확인한다."""
        rules = {r["id"]: re.compile(r["regex"]) for r in gitleaks["rules"]}
        # 표본은 문자열 연결로 만든다. 통짜로 적으면 이 파일 자체가
        # 시크릿 스캔에 걸린다(그게 정상 동작이다).
        assert rules["kis-app-key"].search("PS" + "A1" * 17)
        assert rules["telegram-bot-token"].search("8012345678" + ":" + "Ab3" * 12)
        assert rules["kis-account-no"].search("CANO = " + '"' + "5012" + "3456" + '"')
