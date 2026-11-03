"""KLMS MCP server: read-only KAIST KLMS tools for Codex and other MCP clients."""

from typing import Optional

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


def serve():
    mcp.run(transport="stdio")
