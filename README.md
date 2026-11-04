# klms-mcp

Codex에서 KAIST KLMS를 조회하는 MCP 서버. 과목, 과제, 퀴즈, 공지, 알림, 마감을 읽기만 한다.

## 설치

1. 빈 폴더를 하나 만든다. 예: `~/KLMS`
2. Codex 앱에서 그 폴더를 프로젝트로 연다.
3. 새 대화에 아래를 붙여 넣는다.

> `uvx --from git+https://github.com/hwan809/klms-mcp klms-mcp install` 실행해줘. uv가 없으면 먼저 설치해줘. 끝나면 `klms-mcp keepalive enable --public`도 실행해줘.
> 그다음 이 프로젝트에 평일 아침 9시 자동화를 만들어줘. KLMS MCP의 `klms_sweep`을 `dest_dir`를 이 프로젝트 폴더로 해서 돌리고, 마감이 남은 과제마다 `과목/과제명` 폴더에 첨부를 모으고 마감, 제출 형식, 해야 할 일을 정리한 START_HERE.md를 써줘.

로그인 창이 두 번 뜬다. Chrome에서 KAIST SSO 로그인을 한 번, 브라우저에서 GitHub 로그인 승인을 한 번 하면 된다. GitHub 코드는 클립보드에 들어가 있어서 붙여 넣기만 하면 된다.

터미널에서 직접 하려면 [uv](https://docs.astral.sh/uv/)와 Google Chrome을 준비하고:

```sh
uvx --from git+https://github.com/hwan809/klms-mcp klms-mcp install
klms-mcp keepalive enable --public   # 선택: 노트북이 잠들어도 세션 유지
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
| `klms_sweep` | 한 번에 점검: 새 자료와 과제 첨부 받기, 공지 전문 저장, 마감과 과제 본문, 공지에 적힌 일정, 새 성적 |

`klms_sweep`와 받기 도구는 `dest_dir` 아래에 `과목/lectures`, `과목/assignments`, `과목/notices`로 나눠 저장한다. Codex 프로젝트 폴더를 `dest_dir`로 주면 그 프로젝트에서 바로 "수리통계학 중간고사 분포 보고 기말 공부법 알려줘" 같은 질문을 할 수 있다. 처음 돌 때는 기존 공지를 전부 파일로 받아 두지만 "새 공지"로 알리지는 않는다.

받기는 내 컴퓨터에만 저장한다. 과제 제출 같은 KLMS에 쓰는 도구는 없다.

## 로그인 유지

KLMS 세션은 3시간 동안 요청이 없으면 끊기고, 끊긴 뒤에는 KAIST SSO가 비밀번호를 다시 요구한다. 노트북이 밤새 잠들면 아침에는 보통 다시 로그인해야 한다.
세션이 끊겼으면 Codex가 `klms_login`으로 Chrome 창을 띄운다. 처음 로그인할 때 그 창에서 Chrome 비밀번호 저장을 해 두면, 다음부터는 자동 완성된 상태에서 로그인 버튼만 누르면 된다.
터미널에서는 `klms-mcp status`로 세션을 확인하고 `klms-mcp login`으로 다시 로그인한다.

### GitHub로 세션 살려 두기

```sh
klms-mcp keepalive enable
```

GitHub 계정이 필요하다. [GitHub CLI](https://cli.github.com/)가 없으면 Homebrew(macOS)나 winget(Windows)으로 설치하고, 로그인이 안 돼 있으면 브라우저 승인 창을 연다. 승인 코드는 클립보드에 넣어 둔다. 그다음 내 계정에 리포 `klms-keepalive`를 만들고, GitHub Actions가 30분마다 KLMS를 한 번 열어 세션이 끊기지 않게 한다.

GitHub 예약 실행(cron)은 믿을 수 없다. 매시간으로 걸어 둔 실측에서 13시간 동안 1번만 돌았다. 그래서 cron에 기대지 않고, 실행이 끝날 때 다음 실행을 건다. 다음 실행은 `keepalive` environment의 30분 대기 뒤에 시작하고, 이 대기 시간은 Actions 사용량으로 치지 않는다. cron은 체인이 멈췄을 때 다시 시작하는 용도로만 둔다.

- 대기 타이머는 public 리포에서만 된다(private은 GitHub Enterprise 전용). 그래서 `klms-mcp keepalive enable --public`으로 켠다. `--public` 없이 실행하면 public으로 바꾸기 전에 멈추고 이유를 알려준다.
- public이어도 쿠키는 Secret이라 로그에서 가려지고, fork나 PR에는 전달되지 않는다. 남에게 보이는 것은 워크플로 파일, 실행 시각, "KLMS session alive" 한 줄이다.
- Secret에는 klms.kaist.ac.kr 쿠키만 들어간다. KAIST SSO 쿠키와 비밀번호는 올리지 않는다.
- 세션이 끊기면 실행이 실패하고 GitHub가 메일을 보낸다. 체인도 거기서 멈춘다. `klms-mcp login`(또는 Codex의 `klms_login`)을 하면 Secret을 새 쿠키로 바꾸고 체인을 다시 건다.
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
