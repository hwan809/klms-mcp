"""Keep the KLMS session alive from a private GitHub repo (GitHub Actions, hourly).

KLMS drops a session after 3 idle hours, and a dead session needs a human SSO login.
The laptop sleeps; GitHub Actions does not. Both use the same session cookies, so an
hourly touch from Actions keeps the laptop's session alive too.

Only the klms.kaist.ac.kr cookies go to the repo secret, never the SSO cookies.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser

from klms_mcp import klms

REPO_NAME = "klms-keepalive"
SECRET = "KLMS_COOKIE"
WORKFLOW_PATH = ".github/workflows/keepalive.yml"
STATE = klms._SKILLS_DIR / "keepalive.json"

WORKFLOW = """name: klms-keepalive

on:
  schedule:
    # KLMS drops idle sessions after 3 hours. GitHub skips many scheduled runs,
    # so ask twice an hour (at most ~1,440 min/month, under the free 2,000).
    - cron: "7,37 * * * *"
  workflow_dispatch:

concurrency:
  group: keepalive
  cancel-in-progress: true

permissions: {}

jobs:
  touch:
    runs-on: ubuntu-latest
    timeout-minutes: 3
    steps:
      - name: Touch KLMS session
        env:
          KLMS_COOKIE: ${{ secrets.KLMS_COOKIE }}
        run: |
          code=$(curl -s -o page.html -w '%{http_code}' --max-time 60 \\
            -H "Cookie: $KLMS_COOKIE" -A 'Mozilla/5.0' https://klms.kaist.ac.kr/my/)
          if [ "$code" = 200 ] && grep -q 'page-my-index' page.html; then
            echo "KLMS session alive"
          else
            echo "KLMS session is dead (HTTP $code). On your laptop run: klms-mcp login"
            exit 1
          fi
"""

README = """# klms-keepalive

[klms-mcp](https://github.com/hwan809/klms-mcp) 가 만든 개인 리포. GitHub Actions 가 매시간 KLMS 를 한 번 열어 세션이 끊기지 않게 한다.

- Secret `KLMS_COOKIE` 에 내 KLMS 세션 쿠키가 들어 있다. 리포를 public 으로 바꾸지 말 것.
- 워크플로가 실패하면 세션이 끊긴 것이다. 노트북에서 `klms-mcp login` 을 하면 Secret 도 갱신된다.
- 끄기: `klms-mcp keepalive disable`
"""


def _gh(*args, input=None, check=True):
    return subprocess.run(["gh", *args], input=input, capture_output=True, text=True, check=check)


def _install_gh():
    """gh 가 없으면 패키지 관리자로 깐다. 깔 방법이 없으면 안내하고 멈춘다."""
    if shutil.which("brew"):
        cmd = ["brew", "install", "gh"]
    elif shutil.which("winget"):
        cmd = ["winget", "install", "--id", "GitHub.cli", "-e", "--silent"]
    else:
        raise SystemExit("GitHub CLI(gh)가 필요합니다. https://cli.github.com 에서 설치한 뒤 다시 실행하세요.")
    print(f"GitHub CLI 설치: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    if not shutil.which("gh"):
        raise SystemExit("gh 를 설치했지만 PATH 에서 찾지 못했습니다. 새 터미널에서 다시 실행하세요.")


def _device_login(args: list[str]):
    """gh 의 브라우저 로그인. Codex 같은 비대화형 셸에서도 되도록 코드를 클립보드에 넣고 창을 연다."""
    p = subprocess.Popen(["gh", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env={**os.environ, "BROWSER": "true"})
    opened = False
    for line in p.stdout:
        m = re.search(r"one-time code: ([A-Z0-9]{4}-[A-Z0-9]{4})", line)
        if m and not opened:
            code = m.group(1)
            for clip in (["pbcopy"], ["clip"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
                if shutil.which(clip[0]):
                    subprocess.run(clip, input=code, text=True, check=False)
                    break
            print(f"브라우저에서 GitHub 로그인 후 코드 {code} 를 붙여 넣고 Authorize 를 누르세요. (코드는 클립보드에 있음)",
                  flush=True)
            webbrowser.open("https://github.com/login/device")
            opened = True
    if p.wait() != 0:
        raise SystemExit("GitHub 로그인이 끝나지 않았습니다. 다시 실행하세요.")


def ensure_gh():
    """gh 설치, 로그인, workflow 권한까지 확인한다. 워크플로 파일을 올리려면 workflow 권한이 필요하다."""
    if not shutil.which("gh"):
        _install_gh()
    # --active: 여러 계정이 등록돼 있으면 비활성 계정 권한까지 출력되므로 지금 쓰는 계정만 본다.
    st = _gh("auth", "status", "--active", "--hostname", "github.com", check=False)
    if st.returncode != 0:
        _device_login(["auth", "login", "--hostname", "github.com", "--git-protocol", "https",
                       "--web", "--scopes", "workflow"])
    elif "'workflow'" not in st.stdout + st.stderr:
        _device_login(["auth", "refresh", "--hostname", "github.com", "--scopes", "workflow"])


def _cookie_header() -> str:
    cookies = [c for c in klms._load_cookies() if "klms.kaist.ac.kr" in c.get("domain", "")]
    if not cookies:
        raise SystemExit("KLMS 쿠키가 없습니다. 먼저 klms-mcp login")
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _put_file(repo: str, path: str, content: str, message: str):
    sha = _gh("api", f"repos/{repo}/contents/{path}", "--jq", ".sha", check=False).stdout.strip()
    body = {"message": message, "content": base64.b64encode(content.encode()).decode()}
    if sha and not sha.startswith("{"):
        body["sha"] = sha
    _gh("api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-", input=json.dumps(body))


def _is_private(repo: str) -> bool:
    vis = _gh("repo", "view", repo, "--json", "visibility", "--jq", ".visibility", check=False).stdout.strip()
    return vis == "PRIVATE"


def push_secret(repo: str):
    # 쿠키는 계정 접근권이다. 어떤 경로로 불려도 private 리포에만 넣는다.
    if not _is_private(repo):
        raise SystemExit(f"{repo} 가 private 이 아닙니다. 쿠키를 넣을 수 없습니다.")
    # stdin 으로 넘겨서 값이 명령줄, 로그, 화면 어디에도 남지 않게 한다.
    _gh("secret", "set", SECRET, "--repo", repo, input=_cookie_header())


def _repo() -> str | None:
    try:
        st = json.loads(STATE.read_text())
        return None if st.get("disabled") else st["repo"]
    except (OSError, ValueError, KeyError):
        pass
    # 상태 파일이 없어도(다른 KLMS_MCP_HOME 에서 켰거나 지워졌을 때) 내 계정에 keepalive 리포가
    # 있으면 그걸 쓴다. 이게 없어서 다시 로그인해도 Secret 이 옛 쿠키로 남은 적이 있다.
    if not shutil.which("gh"):
        return None
    user = _gh("api", "user", "--jq", ".login", check=False).stdout.strip()
    repo = f"{user}/{REPO_NAME}"
    if (user and _is_private(repo)
            and _gh("api", f"repos/{repo}/contents/{WORKFLOW_PATH}", "--jq", ".sha", check=False).returncode == 0):
        STATE.write_text(json.dumps({"repo": repo}))
        return repo
    return None


def sync_after_login():
    """login() 뒤에 불린다. keepalive 를 켜 둔 경우에만 Secret 을 새 쿠키로 바꾼다."""
    repo = _repo()
    if not repo or not shutil.which("gh"):
        return None
    try:
        push_secret(repo)
        return repo
    except subprocess.CalledProcessError as e:
        return f"실패: {e.stderr.strip()[:200]}"
    except SystemExit as e:
        return f"건너뜀: {e}"


def _latest_run(repo: str):
    out = _gh("run", "list", "--repo", repo, "--workflow", "keepalive.yml", "--limit", "1",
              "--json", "databaseId,status,conclusion,createdAt").stdout
    runs = json.loads(out or "[]")
    return runs[0] if runs else None


def enable():
    if not klms.check_session().get("session_valid"):
        raise SystemExit("KLMS 세션이 없습니다. 먼저 klms-mcp login")
    ensure_gh()

    user = _gh("api", "user", "--jq", ".login").stdout.strip()
    repo = f"{user}/{REPO_NAME}"
    if _gh("repo", "view", repo, check=False).returncode != 0:
        print(f"[1/4] private 리포 만들기: {repo}")
        _gh("repo", "create", repo, "--private", "--description", "KLMS session keepalive (klms-mcp)")
    else:
        if not _is_private(repo):
            raise SystemExit(f"{repo} 가 private 이 아닙니다. 쿠키를 넣을 수 없습니다.")
        print(f"[1/4] 기존 private 리포 사용: {repo}")

    print("[2/4] 워크플로 올리기")
    _put_file(repo, "README.md", README, "docs: keepalive README")
    _put_file(repo, WORKFLOW_PATH, WORKFLOW, "feat: hourly KLMS keepalive")

    print("[3/4] Secret 넣기 (KLMS 쿠키만, 화면에 출력하지 않음)")
    push_secret(repo)
    STATE.write_text(json.dumps({"repo": repo}))

    print("[4/4] 첫 실행으로 확인")
    before = (_latest_run(repo) or {}).get("databaseId")
    for _ in range(10):  # 새 워크플로가 인식될 때까지 잠깐 걸린다
        if _gh("workflow", "run", "keepalive.yml", "--repo", repo, check=False).returncode == 0:
            break
        time.sleep(3)
    run = None
    for _ in range(60):
        time.sleep(5)
        run = _latest_run(repo)
        if run and run["databaseId"] != before and run["status"] == "completed":
            break
    if not run or run.get("status") != "completed":
        print(f"첫 실행 결과를 기다리지 못했습니다. https://github.com/{repo}/actions 에서 확인하세요.")
        return
    if run["conclusion"] == "success":
        print(f"\n완료. GitHub 가 매시간 KLMS 세션을 살려 둡니다: https://github.com/{repo}/actions")
    else:
        log = _gh("run", "view", str(run["databaseId"]), "--repo", repo, "--log-failed", check=False).stdout
        print(f"\n첫 실행 실패: https://github.com/{repo}/actions/runs/{run['databaseId']}")
        print("\n".join(l for l in log.splitlines() if "KLMS" in l)[-500:])


def disable():
    repo = _repo()
    if not repo:
        print("keepalive 가 켜져 있지 않습니다.")
        return
    _gh("secret", "delete", SECRET, "--repo", repo, check=False)
    _gh("workflow", "disable", "keepalive.yml", "--repo", repo, check=False)
    STATE.write_text(json.dumps({"repo": repo, "disabled": True}))
    print(f"껐습니다. Secret 을 지우고 워크플로를 멈췄습니다. 리포까지 지우려면: gh repo delete {repo}")


def status():
    repo = _repo()
    if not repo:
        print("keepalive 꺼짐")
        return
    run = _latest_run(repo)
    print(f"{repo}: 최근 실행 {run['createdAt'] if run else '-'} {run['conclusion'] if run else ''}")
