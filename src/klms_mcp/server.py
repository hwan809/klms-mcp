"""KLMS MCP server: read-only KAIST KLMS tools for Codex and other MCP clients."""

import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver import MCPServer

from klms_mcp import klms

mcp = MCPServer(
    name="klms",
    description="KAIST KLMS (Moodle) 조회 도구: 과목, 과제, 퀴즈, 공지, 알림, 마감",
)

# Codex 설정에서 자동 승인할 도구. 모두 읽기 전용이고, klms_login 은 내 노트북에 로그인 창만 띄운다.
TOOL_NAMES = []


def tool(description):
    def wrap(fn):
        TOOL_NAMES.append(fn.__name__)
        return mcp.tool(description=description)(fn)
    return wrap


@tool("KLMS 세션(쿠키)이 살아 있는지 확인합니다. 다른 도구보다 먼저 호출하세요. 세션이 없거나 만료됐으면 klms_login 을 호출하세요.")
def klms_check_session() -> dict:
    return klms.check_session()


@tool("이 컴퓨터에 Chrome 창을 띄워 KAIST SSO 로그인을 받고 쿠키를 ~/.klms-mcp 에 저장합니다. 사용자가 창에서 직접 로그인해야 합니다.")
def klms_login(timeout_seconds: int = 180) -> dict:
    return klms.login(timeout_seconds=timeout_seconds)


@tool("현재 수강 중인 과목 목록을 가져옵니다.")
def klms_get_courses() -> dict:
    return klms.get_courses()


@tool("특정 과목의 활동 목록(과제, 퀴즈, 공지, 파일 등)을 가져옵니다. course_name 은 과목명 일부(예: '신호', 'CS311').")
def klms_get_activities(course_name: str) -> dict:
    return klms.get_course_activities(course_name)


@tool("KLMS 알림 목록을 가져옵니다. (새 공지, 과제 피드백, 성적 등)")
def klms_get_notifications(limit: int = 20) -> dict:
    return klms.get_notifications(limit=limit)


@tool("수강 과목 공지 게시판 글을 가져옵니다. 기본은 새 글만, include_seen=True 면 이미 확인한 공지까지 전부.")
def klms_get_notices(course_filter: Optional[str] = None, include_seen: bool = False) -> dict:
    return klms.get_course_notices(course_filter=course_filter, include_seen=include_seen)


@tool("과목 전체 콘텐츠(섹션, 모듈, 파일)를 한 번에 조회합니다. course_id 는 KLMS 과목 ID(예: '184848').")
def klms_course_contents(course_id: str) -> dict:
    return klms.get_course_contents_api(course_id)


@tool("과제 현황(제출 상태, 마감, 성적 등)을 확인합니다. assign_url 은 mod/assign/view.php?id=... 주소.")
def klms_assignment_status(assign_url: str) -> dict:
    return klms.get_assignment_status(assign_url)


@tool("퀴즈 현황(마감, 응시 상태, 성적 등)을 확인합니다. quiz_url 은 mod/quiz/view.php?id=... 주소.")
def klms_quiz_status(quiz_url: str) -> dict:
    return klms.get_quiz_status(quiz_url)


@tool("전체 수강 과목의 다가오는 마감(과제 + 퀴즈)을 한 번에 조회합니다.")
def klms_upcoming_deadlines(days_ahead: int = 14) -> dict:
    return klms.get_upcoming_deadlines(days_ahead=days_ahead)


def _download(session, url: str, dest: Path) -> Path:
    r = session.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()
    name = ""
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    if m:
        name = unquote(m.group(1))
    if not name:
        name = unquote(urlparse(r.url).path.rsplit("/", 1)[-1]) or "attachment"
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    path = dest / name
    path.write_bytes(r.content)
    return path


@tool("과제 설명에 첨부된 파일(문제지, 시작 코드 등)을 dest_dir 에 받습니다. dest_dir 은 작업 폴더의 절대 경로. 없으면 ~/.klms-mcp/downloads.")
def klms_download_assignment(assign_url: str, dest_dir: Optional[str] = None) -> dict:
    status = klms.get_assignment_status(assign_url)
    if not status.get("success"):
        return status
    dest = Path(dest_dir).expanduser() if dest_dir else klms._SKILLS_DIR / "downloads"
    dest.mkdir(parents=True, exist_ok=True)
    session = klms._make_session()
    saved, failed = [], []
    for url in status.get("attachments", []):
        try:
            saved.append(str(_download(session, url, dest)))
        except Exception as e:
            failed.append({"url": url, "error": str(e)})
    return {"success": True, "assignment": status.get("name") or status.get("title"),
            "due_date": status.get("due_date"), "saved": saved, "failed": failed,
            "message": f"첨부 {len(saved)}개 저장 → {dest}" + (f", 실패 {len(failed)}개" if failed else "")}


@tool("수강 과목 자료실의 새 파일을 dest_dir 아래 과목별 폴더로 받습니다. 이미 받은 파일은 건너뜁니다. dest_dir 이 없으면 ~/.klms-mcp/downloads.")
def klms_download_course_files(course_filter: Optional[str] = None, dest_dir: Optional[str] = None) -> dict:
    return klms.download_new_files_fast(course_filter=course_filter, dest_dir=dest_dir)


def serve():
    mcp.run(transport="stdio")
