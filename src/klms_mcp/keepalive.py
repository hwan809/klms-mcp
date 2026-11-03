"""Keep the KLMS session alive from a private GitHub repo (GitHub Actions, hourly).

KLMS drops a session after 3 idle hours, and a dead session needs a human SSO login.
The laptop sleeps; GitHub Actions does not. Both use the same session cookies, so an
hourly touch from Actions keeps the laptop's session alive too.

Only the klms.kaist.ac.kr cookies go to the repo secret, never the SSO cookies.
"""

import base64
import json
import shutil
import subprocess
import time

from klms_mcp import klms

REPO_NAME = "klms-keepalive"
SECRET = "KLMS_COOKIE"
WORKFLOW_PATH = ".github/workflows/keepalive.yml"
STATE = klms._SKILLS_DIR / "keepalive.json"

WORKFLOW = """name: klms-keepalive

on:
  schedule:
    - cron: "17 * * * *"   # every hour; KLMS drops idle sessions after 3 hours
  workflow_dispatch:

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


def push_secret(repo: str):
    # stdin 으로 넘겨서 값이 명령줄, 로그, 화면 어디에도 남지 않게 한다.
    _gh("secret", "set", SECRET, "--repo", repo, input=_cookie_header())


def _repo() -> str | None:
    try:
        return json.loads(STATE.read_text())["repo"]
    except (OSError, ValueError, KeyError):
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


def _latest_run(repo: str):
    out = _gh("run", "list", "--repo", repo, "--workflow", "keepalive.yml", "--limit", "1",
              "--json", "databaseId,status,conclusion,createdAt").stdout
    runs = json.loads(out or "[]")
    return runs[0] if runs else None


def enable():
    if not shutil.which("gh"):
        raise SystemExit("GitHub CLI(gh)가 필요합니다: brew install gh && gh auth login")
    if _gh("auth", "status", check=False).returncode != 0:
        raise SystemExit("gh 로그인이 필요합니다: gh auth login")
    if not klms.check_session().get("session_valid"):
        raise SystemExit("KLMS 세션이 없습니다. 먼저 klms-mcp login")

    user = _gh("api", "user", "--jq", ".login").stdout.strip()
    repo = f"{user}/{REPO_NAME}"
    if _gh("repo", "view", repo, check=False).returncode != 0:
        print(f"[1/4] private 리포 만들기: {repo}")
        _gh("repo", "create", repo, "--private", "--description", "KLMS session keepalive (klms-mcp)")
    else:
        vis = _gh("repo", "view", repo, "--json", "visibility", "--jq", ".visibility").stdout.strip()
        if vis != "PRIVATE":
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
    STATE.unlink(missing_ok=True)
    print(f"껐습니다. Secret 을 지우고 워크플로를 멈췄습니다. 리포까지 지우려면: gh repo delete {repo}")


def status():
    repo = _repo()
    if not repo:
        print("keepalive 꺼짐")
        return
    run = _latest_run(repo)
    print(f"{repo}: 최근 실행 {run['createdAt'] if run else '-'} {run['conclusion'] if run else ''}")
