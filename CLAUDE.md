# CLAUDE.md

이 저장소의 작업 규약은 **[AGENTS.md](AGENTS.md)** 에 있다. 먼저 읽어라.

## 한 줄 요약

소스를 고쳤으면 `python scripts/verify.py` 가 **전부 통과해야 완료**다.
통과하지 않았다면 그 작업은 아직 진행 중이다.

```bash
python scripts/verify.py        # 전체 게이트 (커밋 전 필수)
python scripts/verify.py --fast # 빠른 확인 (개발 중)
python scripts/verify.py --fix  # 자동 수정 후 재검증
```

## 절대 규칙 세 가지

1. **비밀정보를 커밋하지 않는다.** 앱키·앱시크릿·계좌번호·상태DB·로그.
   `verify.py` 의 첫 게이트가 이를 검사하며, 이 게이트는 어떤 경우에도 우회하지 않는다.
2. **테스트를 느슨하게 만들어 통과시키지 않는다.** 코드를 고친다.
3. **`DRY_RUN` 기본값(`true`)과 `KIS_ENV` 기본값(`paper`)을 뒤집지 않는다.**

나머지 상세 규약, 아키텍처, 도메인 함정은 전부 [AGENTS.md](AGENTS.md) 를 보라.
