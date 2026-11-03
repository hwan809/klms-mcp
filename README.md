# klms-mcp

Codex에서 KAIST KLMS를 조회하는 MCP 서버. 과목, 과제, 퀴즈, 공지, 알림, 마감을 읽기만 한다.

## 설치

Codex 앱에 이 한 줄을 붙여 넣는다.

> `uvx --from git+https://github.com/hwan809/klms-mcp klms-mcp install` 실행해줘. uv가 없으면 먼저 설치해줘. 그다음 평일 아침 9시마다 KLMS에서 새 과제를 확인하고, 새 과제가 있으면 `~/KLMS/과목명/과제명` 폴더에 첨부파일을 받고 마감, 제출 형식, 해야 할 일을 정리한 START_HERE.md를 만드는 자동화를 만들어줘.

터미널에서 직접 하려면 [uv](https://docs.astral.sh/uv/)와 Google Chrome을 준비하고:

```sh
uvx --from git+https://github.com/hwan809/klms-mcp klms-mcp install
```

설치 명령이 하는 일:

1. `uv tool install`로 `klms-mcp`를 설치한다.
2. `~/.codex/config.toml`에 `[mcp_servers.klms]`를 등록하고 도구를 자동 승인으로 둔다. 기존 설정은 `config.toml.bak-*`로 백업한다.
3. Chrome 창을 띄워 KAIST SSO 로그인을 받는다.

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
| `klms_download_assignment` | 과제 첨부파일 받기 |
| `klms_download_course_files` | 과목 자료실 새 파일 받기 |

받기는 내 컴퓨터에만 저장한다. 과제 제출 같은 KLMS에 쓰는 도구는 없다.

## 로그인 유지

KLMS 세션은 3시간 동안 요청이 없으면 끊기고, 끊긴 뒤에는 KAIST SSO가 비밀번호를 다시 요구한다. 노트북이 밤새 잠들면 아침에는 보통 다시 로그인해야 한다.
세션이 끊겼으면 Codex가 `klms_login`으로 Chrome 창을 띄운다. 처음 로그인할 때 그 창에서 Chrome 비밀번호 저장을 해 두면, 다음부터는 자동 완성된 상태에서 로그인 버튼만 누르면 된다.
터미널에서는 `klms-mcp status`로 세션을 확인하고 `klms-mcp login`으로 다시 로그인한다.

### GitHub로 세션 살려 두기

```sh
klms-mcp keepalive enable
```

[GitHub CLI](https://cli.github.com/)로 내 계정에 private 리포 `klms-keepalive`를 만들고, GitHub Actions가 매시간 KLMS를 한 번 열어 세션이 끊기지 않게 한다. 노트북이 잠들어도 세션이 살아 있다.

- Secret에는 klms.kaist.ac.kr 쿠키만 들어간다. KAIST SSO 쿠키는 올리지 않는다.
- 이 쿠키는 내 KLMS 계정 접근권이다. 리포를 public으로 바꾸지 않는다.
- 세션이 끊기면 워크플로가 실패하고 GitHub가 메일을 보낸다. `klms-mcp login`을 하면 Secret도 새 쿠키로 바뀐다.
- private 리포 Actions 무료 한도(월 2,000분) 안에서 월 약 720분을 쓴다.
- 끄기: `klms-mcp keepalive disable`

## 데이터

- 쿠키, 상태, 로그인 브라우저 프로필은 모두 이 컴퓨터의 `~/.klms-mcp/`에만 있다. 다른 곳으로 보내지 않는다.
- 위치를 바꾸려면 `KLMS_MCP_HOME` 환경변수를 쓴다.

## 제거

```sh
klms-mcp uninstall          # Codex 설정에서 뺀다
uv tool uninstall klms-mcp  # 프로그램을 지운다
rm -rf ~/.klms-mcp          # 쿠키와 상태를 지운다
```
