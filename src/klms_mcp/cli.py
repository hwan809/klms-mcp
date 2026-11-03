"""klms-mcp            -> run the MCP server (stdio)
klms-mcp install    -> install as a uv tool and register it in Codex (~/.codex/config.toml)
klms-mcp uninstall  -> remove it from Codex config
klms-mcp login      -> open Chrome for KAIST SSO login
klms-mcp status     -> check whether the saved KLMS session is alive
klms-mcp keepalive enable|disable|status
                    -> keep the session alive from your private GitHub repo (Actions, hourly)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

SERVER = "klms"


def _codex_config() -> Path:
    home = Path(os.environ.get("CODEX_HOME", "") or Path.home() / ".codex")
    home.mkdir(parents=True, exist_ok=True)
    return home / "config.toml"


def _source_spec() -> str:
    """Where this copy came from, so `uv tool install` can install the same thing."""
    raw = metadata.distribution("klms-mcp").read_text("direct_url.json")
    if not raw:
        raise SystemExit("설치 출처를 알 수 없습니다. README 의 설치 명령으로 실행하세요.")
    d = json.loads(raw)
    url = d["url"]
    if "vcs_info" in d:
        return f"git+{url}"
    if url.startswith("file://"):
        return url[len("file://"):]
    return url


def _strip_server(text: str) -> str:
    """Drop [mcp_servers.klms] and [mcp_servers.klms.*] tables, keep everything else."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        m = re.match(r"\s*\[([^\]]+)\]\s*$", line)
        if m:
            name = m.group(1).strip()
            skip = name == f"mcp_servers.{SERVER}" or name.startswith(f"mcp_servers.{SERVER}.")
        if not skip:
            out.append(line)
    return "".join(out).rstrip() + "\n"


def install():
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv 가 필요합니다: curl -LsSf https://astral.sh/uv/install.sh | sh")
    spec = _source_spec()
    print(f"[1/3] uv tool install {spec}")
    subprocess.run([uv, "tool", "install", "--force", spec], check=True)
    bin_dir = subprocess.run([uv, "tool", "dir", "--bin"], check=True, capture_output=True, text=True).stdout.strip()
    exe = Path(bin_dir) / ("klms-mcp.exe" if os.name == "nt" else "klms-mcp")
    if not exe.exists():
        raise SystemExit(f"설치된 실행 파일을 찾지 못했습니다: {exe}")

    from klms_mcp.server import TOOL_NAMES

    cfg = _codex_config()
    old = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    if old:
        backup = cfg.with_name(f"config.toml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(old, encoding="utf-8")
        print(f"[2/3] 기존 설정 백업: {backup}")
    block = [f"\n[mcp_servers.{SERVER}]", f"command = {json.dumps(str(exe))}", "startup_timeout_sec = 30"]
    for name in TOOL_NAMES:
        block += [f"\n[mcp_servers.{SERVER}.tools.{name}]", 'approval_mode = "approve"']
    cfg.write_text(_strip_server(old) + "\n".join(block) + "\n", encoding="utf-8")
    print(f"[3/3] Codex 설정에 등록: {cfg}")

    from klms_mcp import klms
    if "--no-login" not in sys.argv and not klms.check_session().get("session_valid"):
        print("\nChrome 창에서 KAIST SSO 로그인을 마쳐 주세요. (최대 3분)")
        r = klms.login(timeout_seconds=180)
        print("로그인 완료" if r.get("success") else f"로그인 실패: {r.get('error')} (나중에 klms_login 으로 다시)")
    print("\n완료. Codex 새 대화에서 이렇게 말해 보세요:\n  이번 주 KLMS 마감 알려줘")


def uninstall():
    cfg = _codex_config()
    if cfg.exists():
        cfg.write_text(_strip_server(cfg.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"Codex 설정에서 뺐습니다: {cfg}\n프로그램까지 지우려면: uv tool uninstall klms-mcp")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "login":
        from klms_mcp import klms
        print("Chrome 창에서 KAIST SSO 로그인을 마쳐 주세요. (최대 3분)")
        r = klms.login(timeout_seconds=180)
        print("로그인 완료" if r.get("success") else f"로그인 실패: {r.get('error')}")
        sys.exit(0 if r.get("success") else 1)
    elif cmd == "keepalive":
        from klms_mcp import keepalive
        sub = sys.argv[2] if len(sys.argv) > 2 else "status"
        {"enable": keepalive.enable, "disable": keepalive.disable, "status": keepalive.status}[sub]()
    elif cmd == "status":
        from klms_mcp import klms
        r = klms.check_session()
        print("세션 살아 있음" if r.get("session_valid") else f"로그인 필요: {r.get('error', '')}")
        sys.exit(0 if r.get("session_valid") else 1)
    elif cmd == "serve":
        from klms_mcp.server import serve
        serve()
    else:
        print(__doc__)
        sys.exit(1)
