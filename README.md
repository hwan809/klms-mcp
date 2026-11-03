# klms-mcp

Codex에서 KAIST KLMS를 조회하는 MCP 서버. 과목, 과제, 퀴즈, 공지, 알림, 마감을 읽기만 한다.

## 설치

[uv](https://docs.astral.sh/uv/)와 Google Chrome이 필요하다.

```sh
uvx --from git+https://github.com/hwan809/klms-mcp klms-mcp install
```

또는 Codex에 그대로 말하면 된다.

> `uvx --from git+https://github.com/hwan809/klms-mcp klms-mcp install` 실행해줘. uv가 없으면 먼저 설치해줘.

설치 명령이 하는 일:

1. `uv tool install`로 `klms-mcp`를 설치한다.
2. `~/.codex/config.toml`에 `[mcp_servers.klms]`를 등록하고 조회 도구를 자동 승인으로 둔다. 기존 설정은 `config.toml.bak-*`로 백업한다.

## 사용

Codex를 새로 열고:

> KLMS 로그인하고 이번 주 마감 알려줘

처음 한 번은 Chrome 창이 뜬다. 창에서 KAIST SSO 로그인을 마치면 쿠키가 `~/.klms-mcp/cookies.json`에 저장된다. 세션이 만료되면 다시 로그인 창을 띄운다.

## 도구

| 도구 | 내용 |
|---|---|
| `klms_check_session` | 세션 확인 |
| `klms_login` | Chrome으로 SSO 로그인 |
| `klms_get_courses` | 수강 과목 |
| `klms_get_activities` | 과목별 활동 |
| `klms_get_notifications` | 알림 |
| `klms_get_notices` | 공지 게시판 |
| `klms_course_contents` | 과목 전체 콘텐츠 |
| `klms_assignment_status` | 과제 상태 |
| `klms_quiz_status` | 퀴즈 상태 |
| `klms_upcoming_deadlines` | 다가오는 마감 |

과제 제출 같은 쓰기 도구는 없다.

## 데이터

- 쿠키, 상태, 로그인 브라우저 프로필은 모두 이 컴퓨터의 `~/.klms-mcp/`에만 있다. 다른 곳으로 보내지 않는다.
- 위치를 바꾸려면 `KLMS_MCP_HOME` 환경변수를 쓴다.

## 제거

```sh
klms-mcp uninstall          # Codex 설정에서 뺀다
uv tool uninstall klms-mcp  # 프로그램을 지운다
rm -rf ~/.klms-mcp          # 쿠키와 상태를 지운다
```
