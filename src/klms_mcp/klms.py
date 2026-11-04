"""
klms_tools.py
-------------
KAIST KLMS 통합 툴킷: 세션 확인, 알림, 과목별 공지, 파일 다운로드, 과제 제출.

환경변수 (.env):
  KLMS_BASE_URL      = https://klms.kaist.ac.kr  (기본값)
  KLMS_COOKIES_PATH  = cookies.json 경로 (기본: 이 파일과 같은 디렉터리)
  KLMS_DOWNLOAD_DIR  = 파일 다운로드 경로 (기본: klms_skills/downloads/)

인증 방식:
  KLMS은 KAIST SSO 기반이라 API 토큰이 없음.
  klms_login 으로 한 번 로그인하면 ~/.klms-mcp/cookies.json 에 저장해 사용.
  세션 만료 시 login() 함수 호출로 쿠키를 갱신합니다.

  로그인 방법:
    from klms_skills.klms_tools import login
    login()   # 브라우저가 열리고 KAIST SSO 로그인 후 자동 저장

반환 형식:
  모든 함수는 {"success": True/False, ...} 딕셔너리를 반환합니다.
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# ── 경로 설정 ─────────────────────────────────────────────────────────────

# 사용자 데이터(쿠키, 상태, 로그인 브라우저 프로필)는 패키지 밖 ~/.klms-mcp 에 둔다.
_SKILLS_DIR = Path(os.environ.get("KLMS_MCP_HOME", "") or Path.home() / ".klms-mcp")
_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
_ENV_PATH = _SKILLS_DIR / ".env"


def _load_env():
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

KLMS_BASE = os.environ.get("KLMS_BASE_URL", "https://klms.kaist.ac.kr").rstrip("/")
_COOKIES_PATH = Path(os.environ.get("KLMS_COOKIES_PATH", "") or _SKILLS_DIR / "cookies.json")
_LOCAL_DOWNLOAD_DIR = Path(os.environ.get("KLMS_DOWNLOAD_DIR", "") or _SKILLS_DIR / "downloads")

_COURSE_FOLDER_MAP: dict[str, str] = {}


def _get_download_dir() -> Path:
    """dest_dir 을 안 줬을 때 쓰는 기본 위치."""
    return _LOCAL_DOWNLOAD_DIR


def _course_dest_dir(course_name: str, base: Path) -> Path:
    folder = _COURSE_FOLDER_MAP.get(course_name)
    if folder:
        return base / folder
    safe = re.sub(r'[\\/:*?"<>|]', "_", course_name)
    return base / safe

# 처음 받는 파일은 참고할 로컬 사본이 없다. 그때 과목 폴더 최상위에 떨어뜨리면
# 사람이 매번 손으로 옮겨야 한다 (실측: 2026-09-08 sweep 의 CS311 lecture2-ch1.pdf).
# 파일명으로 갈 곳을 정해준다. 이미 분류해 넣어둔 사본이 있으면 그쪽이 우선이라
# 사람이 옮겨둔 자리를 이 규칙이 되돌리는 일은 없다.

# 'hw' 는 두 글자뿐이라 부분문자열로 찾으면 엉뚱한 파일명에 걸린다. 토큰으로만 본다.
# 단 \b 는 '_' 를 단어 문자로 쳐서 'MAS 355_2026_HW2.pdf' 를 놓쳤다 — 영숫자만 경계로 본다.
_SUBDIR_RULES = (
    ("syllabus", r"syllabus|강의계획"),
    ("assignments", r"homework|assignment|problem[ _-]?set|pset|recitation"
                    r"|과제|숙제|연습문제|(?<![a-z0-9])hw(?![a-z])"),
    ("textbooks", r"textbook|교재"),
)


def _dest_subdir(filename: str) -> str:
    """파일명을 보고 들어갈 하위 폴더를 고른다. 못 고르면 lectures/."""
    name = filename.lower()
    for subdir, pattern in _SUBDIR_RULES:
        if re.search(pattern, name):
            return subdir
    return "lectures"


# ── 파일 갱신 감지 ───────────────────────────────────────────────────────
# KLMS 는 자료를 고쳐 올려도 파일명이 그대로다. 파일명만 보고 건너뛰면 로컬은
# 영원히 구버전으로 남는다 (EE202 'Lecture 1 Updated' 가 실제 사례). 그래서
# 원격 지문을 state 에 남기고 바뀌었을 때만 다시 받는다.

_FP_KEYS = ("revision", "length", "last_modified", "etag")


def _fingerprint_from_url(file_url: str) -> dict:
    """pluginfile URL 에서 Moodle 개정 번호를 뽑는다.

    mod_resource 는 .../mod_resource/content/<revision>/<name> 형태이고, 자료를
    재업로드하면 이 숫자가 올라간다. 네트워크 없이 얻는 가장 싼 신호다.
    게시판 첨부(mod_courseboard)에는 없다 — 그때는 None.
    """
    m = re.search(r"/mod_\w+/content/(\d+)/", file_url)
    return {"revision": m.group(1) if m else None}


def _remote_fingerprint(session: requests.Session, file_url: str) -> dict:
    """HEAD 로 크기·수정시각을 확인한다. 본문을 받지 않는다."""
    fp = _fingerprint_from_url(file_url)
    fp.update({"length": None, "last_modified": None, "etag": None})
    try:
        r = session.head(file_url, allow_redirects=True, timeout=HTTP_TIMEOUT)
        # HEAD 를 막아둔 엔드포인트가 있다. 그때는 개정 번호만으로 판단한다.
        if r.status_code < 400:
            h = r.headers
            fp["length"] = h.get("Content-Length")
            fp["last_modified"] = h.get("Last-Modified")
            fp["etag"] = h.get("ETag")
            rev = _fingerprint_from_url(r.url).get("revision")
            if rev:
                fp["revision"] = rev
    except Exception:
        pass
    return fp


def _should_download(
    local_exists: bool,
    local_size: int,
    stored: Optional[dict],
    remote: dict,
) -> tuple[bool, str]:
    """받아야 하는가? (받을지, 이유) 를 돌려준다.

    이유는 그대로 보고에 실린다 — 'new'(신규) / 'updated'(원격 변경) /
    'stale'(추적 안 되던 파일인데 크기가 다름) / 'unchanged' / 'adopted'.
    """
    if not local_exists:
        return True, "new"

    if stored is None:
        # 이미 받아둔 파일들. 크기가 맞으면 최신으로 간주하고 지문만 기록한다.
        # 이 분기가 없으면 첫 실행에서 학기 전체를 다시 받는다.
        remote_len = remote.get("length")
        if remote_len is None:
            return False, "unknown"
        try:
            same = int(remote_len) == int(local_size)
        except (TypeError, ValueError):
            return False, "unknown"
        return (False, "adopted") if same else (True, "stale")

    for k in _FP_KEYS:
        rv, sv = remote.get(k), stored.get(k)
        if rv is not None and sv is not None and rv != sv:
            return True, "updated"
    return False, "unchanged"


# ── 과목 폴더 훑기 ───────────────────────────────────────────────────────
# 받은 자료는 lectures/ · syllabus/ · assignments/ · textbooks/ 로 분류해 넣는다.
# 최상위만 보면 분류해 넣은 파일을 매번 '신규'로 다시 받는다 (실측: 2026-09-07
# sweep 25개 중 22개가 이미 있던 파일이었다). 보관본과 캐시는 '이미 있는 자료'가
# 아니므로 제외한다 — 보관본을 세면 갱신본을 영영 안 받게 된다.

_SKIP_DIR_NAMES = {"_superseded", "__pycache__"}


def _iter_course_files(course_dir: Path):
    """과목 폴더 아래 자료 파일을 재귀로 훑는다."""
    if not course_dir.is_dir():
        return
    for path in sorted(course_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        parents = path.relative_to(course_dir).parts[:-1]
        if any(d in _SKIP_DIR_NAMES or d.startswith(".") for d in parents):
            continue
        yield path


def _resource_key(f: dict, filename: str) -> str:
    """이 자료를 추적할 키. Moodle activity id 가 있으면 그것을 쓴다.

    파일명으로 추적하면 교수가 고쳐 올리며 이름이 바뀔 때(실측:
    '3. optimization (3).pdf' -> '(4).pdf') 같은 자료의 개정본을 '신규'로 잡아
    구버전 옆에 신버전이 쌓인다. activity id 는 재업로드에도 그대로다.
    게시판 첨부처럼 id 가 없으면 예전 방식으로 돌아간다.
    """
    m = re.search(r"/mod/\w+/view\.php\?id=(\d+)", f.get("view_url", "") or "")
    return f"mod:{m.group(1)}" if m else f"{f.get('course', '')}/{filename}"


def _find_local_copy(course_dir: Path, filename: str) -> Optional[Path]:
    """같은 이름의 파일이 하위 폴더에 있으면 그 경로를 돌려준다."""
    target = filename.lower()
    for path in _iter_course_files(course_dir):
        if path.name.lower() == target:
            return path
    return None


def _discard_if_identical(archived: Optional[Path], current: Path) -> bool:
    """보관본과 새로 받은 파일의 내용이 같으면 보관본을 버린다.

    Last-Modified 는 교수가 파일을 안 고치고 다시 올리기만 해도 바뀐다.
    그때마다 '갱신됨' 이라고 보고하면 진짜 개정과 구분이 안 된다.
    """
    if archived is None or not archived.exists() or not current.exists():
        return False
    h = lambda p: hashlib.md5(p.read_bytes()).hexdigest()
    if h(archived) == h(current):
        archived.unlink()
        parent = archived.parent
        if parent.name == "_superseded" and not any(parent.iterdir()):
            parent.rmdir()
        return True
    return False


def _dedupe_against_dir(fresh: Path, root: Optional[Path] = None) -> Optional[Path]:
    """방금 받은 파일과 내용이 같은 파일이 이미 있으면 중복본을 버린다.

    사용자가 자료를 알아보기 쉬운 이름으로 바꿔 하위 폴더로 정리해 두는 경우가
    있다 ('1. introduction (5).pdf' → 'lectures/introduction.pdf'). 파일명으로만
    대조하면 매번 원본명으로 다시 받아 중복이 쌓인다. 내용으로 알아본다.
    """
    if not fresh.is_file():
        return None
    fresh_hash = hashlib.md5(fresh.read_bytes()).hexdigest()
    for other in _iter_course_files(root or fresh.parent):
        if other == fresh:
            continue
        if other.stat().st_size != fresh.stat().st_size:
            continue
        if hashlib.md5(other.read_bytes()).hexdigest() == fresh_hash:
            fresh.unlink()
            return other
    return None


def _supersede(path: Path) -> Optional[Path]:
    """덮어쓰기 전에 이전 버전을 _superseded/ 로 옮긴다.

    필기나 주석이 붙어 있을 수 있어서 조용히 지우지 않는다.
    """
    if not path.exists():
        return None
    archive_dir = path.parent / "_superseded"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = archive_dir / f"{path.stem}.{stamp}{path.suffix}"
    path.replace(target)
    return target


def _file_exists_locally(course_name: str, filename: str, base: Path) -> bool:
    return _find_local_copy(_course_dest_dir(course_name, base), filename) is not None
_STATE_DIR = _SKILLS_DIR / "state"
# 브라우저 프로필을 유지해 SSO 세션을 재사용한다. KLMS 는 SSO 전용이고 Moodle 토큰
# 경로(token.php / launch.php)가 모두 막혀 있어 쿠키 갱신이 유일한 방법인데,
# 매번 새 컨텍스트를 쓰면 갱신할 때마다 SSO + 2FA 를 처음부터 다시 해야 한다.
_PROFILE_DIR = _SKILLS_DIR / "browser-profile"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}

NOTICE_BOARD_KEYWORDS = ("notice", "공지", "announcement")
# 모든 과목에 자동으로 붙는 LTI — 마감과 무관하므로 "외부 도구" 경고에서 제외한다.
# (실측: 26f 전 과목에 "Panopto Course Tool 외부 도구"가 하나씩 달려 있다)
_LTI_NOISE_KEYWORDS = ("panopto", "zoom", "attendance", "출석")


def _is_graded_lti(activity: dict) -> bool:
    """마감이 있을 수 있는 외부 도구(elice, Gradescope 등)만 True."""
    name = (activity.get("name") or "").lower()
    return not any(k in name for k in _LTI_NOISE_KEYWORDS)
FETCH_DELAY = 0.4

# KLMS 목록 페이지는 20초로 종종 모자랐다 — 매 실행마다 과목 하나가 무작위로
# read timeout 에 걸려 빠졌다. 올려두고, 그래도 실패하면 재시도한다.
HTTP_TIMEOUT = 30
COURSE_LIST_RETRIES = 1


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────

def _ok(data: dict) -> dict:
    return {"success": True, **data}


def _err(msg: str) -> dict:
    return {"success": False, "error": msg}


def _norm_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return KLMS_BASE + href
    return href if href.startswith("http") else ""


def _load_state(name: str) -> dict:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _STATE_DIR / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_state(name: str, data: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    (_STATE_DIR / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 세션 ─────────────────────────────────────────────────────────────────

def _load_cookies() -> list[dict]:
    if not _COOKIES_PATH.exists():
        raise FileNotFoundError(
            f"cookies.json 없음: {_COOKIES_PATH}\n"
            "아직 로그인하지 않았습니다. klms_login 도구를 호출해 KAIST SSO 로그인을 하세요."
        )
    payload = json.loads(_COOKIES_PATH.read_text(encoding="utf-8"))
    return payload.get("cookies", payload) if isinstance(payload, dict) else payload


# ── 쿠키 write-back ──────────────────────────────────────────────────────
# KLMS 세션은 절대 만료가 아니라 유휴 타임아웃으로 죽는다. KLMS 가 내려주는
# M.cfg.sessiontimeout 이 10800 — 3시간 놀면 끝이다. 그래서 요청을 보낼 때마다
# 두 가지를 파일에 남긴다: 서버가 회전시킨 세션 쿠키, 그리고 마지막 접촉 시각.
# 매 요청마다 디스크를 때리면 병렬 크롤링에서 낭비이므로 쿠키 값이 바뀌었거나
# 마지막 기록이 오래됐을 때만 쓴다.

_SESSION_TIMEOUT_HOURS = 3.0   # KLMS M.cfg.sessiontimeout = 10800초
_TOUCH_INTERVAL_SEC = 300.0
_IDLE_WARN_HOURS = 2.0         # 만료(3시간)를 맞기 전에 알린다

_persist_lock = threading.Lock()
_persist_state: dict = {"ts": 0.0, "snapshot": None}


def _persist_session(session) -> bool:
    """세션 쿠키와 마지막 접촉 시각을 cookies.json 에 반영한다. (실제로 썼으면 True)

    기존 쿠키는 갱신·추가만 하고 지우지 않는다 — requests 의 쿠키 자에는 이번
    요청 도메인의 쿠키만 담겨 있을 수 있어서, 없는 것을 삭제로 해석하면 SSO
    쿠키를 통째로 날리게 된다.
    """
    snapshot = {c.name: c.value for c in session.cookies}
    now = time.time()
    if (snapshot == _persist_state["snapshot"]
            and now - _persist_state["ts"] < _TOUCH_INTERVAL_SEC):
        return False

    with _persist_lock:
        try:
            payload = json.loads(_COOKIES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if isinstance(payload, list):
            payload = {"meta": {}, "cookies": payload}
        cookies = payload.get("cookies") or []
        meta = payload.setdefault("meta", {})

        by_name = {c.get("name"): c for c in cookies}
        for c in session.cookies:
            existing = by_name.get(c.name)
            if existing is None:
                cookies.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": (c.domain or "").lstrip("."),
                    "path": c.path or "/",
                })
            else:
                existing["value"] = c.value

        meta["last_seen_epoch"] = now
        meta["last_seen_iso"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        payload["cookies"] = cookies

        tmp = _COOKIES_PATH.with_name(_COOKIES_PATH.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _COOKIES_PATH)
        _persist_state.update(ts=now, snapshot=snapshot)
        return True


class _KlmsSession(requests.Session):
    """요청이 끝날 때마다 쿠키·접촉 시각을 파일에 되쓰는 세션."""

    def request(self, *args, **kwargs):
        response = super().request(*args, **kwargs)
        try:
            _persist_session(self)
        except Exception:
            pass  # 쿠키 저장 실패가 조회 자체를 깨뜨리면 안 된다
        return response


def _make_session() -> requests.Session:
    raw = _load_cookies()
    jar = requests.cookies.RequestsCookieJar()
    for c in raw:
        domain = c.get("domain", "")
        if domain.startswith("."):
            domain = domain[1:]
        jar.set(c["name"], c["value"], domain=domain, path=c.get("path", "/"))
    s = _KlmsSession()
    s.headers.update(_HEADERS)
    s.cookies = jar
    return s


# Moodle 은 세션 잠금을 못 얻으면 503 과 함께 이 문구를 돌려준다. 서버 전체 장애가
# 아니라 '우리 세션 하나'가 잠긴 것이다 — 쿠키 없이 접속하면 200 이 온다. 클라이언트가
# 먼저 타임아웃돼도 서버는 요청을 계속 처리하며 잠금을 붙들고 있어서 이렇게 된다.
# 사람이 재로그인해 새 세션을 받는 것 말고는 푸는 방법이 없다.
_SESSION_LOCK_MARKER = "Unable to obtain session lock"
def _read_text(path: Path) -> str:
    """UTF-8 로 읽는다. Windows 에서 예전 버전이 CP949 등 로캘 인코딩으로 쓴 파일도 읽는다.
    줄바꿈은 read_text() 처럼 \n 으로 맞춘다(Windows 는 \r\n 으로 저장한다)."""
    return _decode(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        import locale
        try:
            return raw.decode(locale.getpreferredencoding(False))
        except (UnicodeDecodeError, LookupError):
            return raw.decode("utf-8", errors="replace")


_LOCK_HINT = ("세션 잠금(Moodle session lock) — klms_login 으로 새 세션을 받으세요. "
              "서버 장애가 아니라 이 세션만 잠긴 상태입니다")


def _is_session_locked(html: str) -> bool:
    return _SESSION_LOCK_MARKER in (html or "")


def _is_session_valid(url: str, html: str, require_dashboard: bool = False) -> bool:
    """세션 유효성 판정.

    require_dashboard=True 는 대시보드(/my/)를 조회한 경우에만 쓴다. 이때는 강의 목록
    같은 긍정 표식이 반드시 있어야 하므로, 표식이 없으면 만료로 본다(fail-closed).

    과제·게시판 등 일반 페이지에는 대시보드 표식이 없는 게 정상이다. 그런 페이지까지
    fail-closed 로 판정하면 멀쩡한 세션을 만료라고 오판한다. 이 경우 신뢰할 수 있는
    신호는 '로그인 페이지로 튕겼는가' 뿐이다.
    """
    # 잠긴 세션은 로그인 페이지로 튕기지도, 로그인 폼을 주지도 않는다. 아래의
    # fail-open 분기에 걸려 '유효'로 새어나가므로 가장 먼저 잡는다.
    if _is_session_locked(html):
        return False

    # 만료 시 KLMS는 /login/ssologin.php?errorcode=4 로 보낸다 — /login/ 경로 전체를 잡는다.
    if "sso.kaist.ac.kr" in url or "/login/" in url:
        return False

    signals = ("강의 현황", "main-course-list", 'id="page-my-index"', "userloggedin", "data-userid=")
    if any(s in html for s in signals):
        return True
    if "<form" in html.lower() and 'type="password"' in html.lower():
        return False

    return not require_dashboard


def _get_sesskey(session: requests.Session) -> str:
    r = session.get(f"{KLMS_BASE}/my/", timeout=15)
    m = re.search(r'"sesskey"\s*:\s*"([^"]+)"', r.text)
    if not m:
        m = re.search(r"sesskey=([a-zA-Z0-9]+)", r.text)
    return m.group(1) if m else ""


# ── 공개 API: 세션 ────────────────────────────────────────────────────────

def _launch_browser(p, **kw):
    """설치된 Chrome, Edge 순으로 띄우고, 없으면 Playwright 가 받은 Chromium 을 쓴다."""
    errors = []
    for channel in ("chrome", "msedge", None):
        try:
            if channel:
                return p.chromium.launch_persistent_context(str(_PROFILE_DIR), channel=channel, **kw)
            return p.chromium.launch_persistent_context(str(_PROFILE_DIR), **kw)
        except Exception as e:  # 브라우저가 없으면 다음 후보
            errors.append(f"{channel or 'chromium'}: {str(e).splitlines()[0]}")
    raise RuntimeError(
        "로그인 창을 띄울 브라우저가 없습니다. Google Chrome 을 설치하거나 "
        "`uvx --from playwright playwright install chromium` 을 실행하세요. (" + " / ".join(errors) + ")"
    )


def login(timeout_seconds: int = 180, headless: bool = False) -> dict:
    """
    브라우저를 열어 KAIST SSO 로그인 후 cookies.json을 저장합니다.
    세션 만료 시 이 함수를 호출해 쿠키를 갱신하세요.

    무인 갱신은 불가능하다. 저장된 SSO 쿠키(authTk, IdP JSESSIONID)를 새 컨텍스트에
    주입해 SSO 로그인 URL 로 직접 들어가도 비밀번호 입력 폼이 뜬다 — 즉 KLMS 세션이
    죽으면 사람이 한 번 로그인해야 한다. headless=True 는 그 사실을 확인하거나
    디버깅할 때 쓰는 옵션이고, 실제 갱신 경로가 아니다.

    Args:
        timeout_seconds: 로그인 대기 시간 (기본 3분)
        headless: True 면 창 없이 시도한다 (SSO 가 사람을 요구하므로 보통 실패)

    Returns:
        {"success": True, "cookies_count": int, "saved_to": str}
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _err(
            "playwright가 설치되지 않았습니다.\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    import time as _time

    try:
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            # 영속 프로필. SSO 의 '이 기기 기억' 세션이 남아 있으면 재로그인이
            # 자동 리다이렉트로 끝나고 2FA 를 다시 하지 않아도 된다.
            context = _launch_browser(
                p,
                headless=headless,
                slow_mo=0 if headless else 100,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="ko-KR",
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{KLMS_BASE}/my/")

            try:
                page.wait_for_url(
                    f"{KLMS_BASE}/my/**",
                    timeout=timeout_seconds * 1000,
                )
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                page.wait_for_selector(
                    "ul.main-course-list, .course-list, #page-my-index",
                    timeout=timeout_seconds * 1000,
                )

            final_url = page.url
            if "sso.kaist.ac.kr" in final_url or "/login/" in final_url:
                context.close()
                return _err(f"로그인 미완료. 현재 URL: {final_url}")

            cookies = context.cookies()
            context.close()

        saved_at = _time.time()
        payload = {
            "meta": {
                "saved_at_epoch": saved_at,
                "saved_at_iso": datetime.fromtimestamp(saved_at, tz=timezone.utc).isoformat(),
                "dashboard_url": final_url,
                "source": "playwright/chromium",
            },
            "cookies": cookies,
        }
        _COOKIES_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _persist_state.update(ts=0.0, snapshot=None)
        mirror = "꺼짐"
        try:  # keepalive 를 켜 둔 사람만: GitHub Secret 을 새 쿠키로 바꾼다
            from klms_mcp.keepalive import sync_after_login
            mirror = sync_after_login() or mirror
        except Exception as e:
            mirror = f"keepalive 갱신 실패: {e}"
        return _ok({
            "cookies_count": len(cookies),
            "saved_to": str(_COOKIES_PATH),
            "headless": headless,
            "mirror": mirror,
            "message": f"쿠키 {len(cookies)}개 저장 완료 → {_COOKIES_PATH} / 미러: {mirror}",
        })
    except Exception as e:
        return _err(str(e))


def session_age_hours() -> Optional[float]:
    """쿠키를 저장한 뒤 지난 시간.

    KLMS 세션 쿠키에는 만료 시각이 없어서, 나이가 유일하게 쓸 수 있는 신호다.
    """
    try:
        payload = json.loads(_COOKIES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    saved = (payload.get("meta") or {}).get("saved_at_epoch")
    if not saved:
        return None
    return max(0.0, (time.time() - float(saved)) / 3600.0)


def session_idle_hours() -> Optional[float]:
    """마지막으로 KLMS 에 접촉한 뒤 지난 시간.

    세션은 유휴 타임아웃으로 죽으므로, 만료가 임박했는지를 말해주는 건 로그인
    후 경과(session_age_hours)가 아니라 이 값이다. 접촉 기록이 없으면 로그인
    시각으로 대신한다.
    """
    try:
        payload = json.loads(_COOKIES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    meta = payload.get("meta") or {}
    stamp = meta.get("last_seen_epoch") or meta.get("saved_at_epoch")
    if not stamp:
        return None
    return max(0.0, (time.time() - float(stamp)) / 3600.0)


def _probe() -> tuple[bool, str, bool]:
    """대시보드를 한 번 조회해 세션 유효성을 확인하고 접촉 시각을 갱신한다.

    Returns: (유효한가, 최종 URL, 잠겼는가)
    """
    session = _make_session()
    r = session.get(f"{KLMS_BASE}/my/", allow_redirects=True, timeout=HTTP_TIMEOUT)
    valid = _is_session_valid(r.url, r.text, require_dashboard=True)
    if valid:
        _persist_session(session)
    return valid, r.url, _is_session_locked(r.text)


def keepalive() -> dict:
    """유휴 타이머를 리셋해 세션을 살려둔다.

    KLMS 세션은 절대 만료가 아니라 유휴 타임아웃(3시간)으로 죽는다. 그보다 짧은
    주기로 가벼운 요청을 한 번씩 보내면 사실상 무기한 유지된다.

    이미 죽었으면 사람이 로그인하는 수밖에 없다. 여기서는 실패로 보고만 한다.

    Returns:
        {"success": bool, "session_valid": bool, "idle_hours": float|None}
    """
    idle = session_idle_hours()

    try:
        valid, final_url, locked = _probe()
    except FileNotFoundError:
        # 쿠키 파일이 아예 없는 것과 세션이 죽은 것은 할 일이 다르다.
        return {"session_valid": False, "idle_hours": None, "final_url": "",
                "checked_at": datetime.now(tz=timezone.utc).isoformat(),
                "success": False,
                "error": f"cookies.json 없음: {_COOKIES_PATH} — klms_login 으로 "
                         "만든 쿠키를 이 경로에 넣어 주세요."}
    except Exception as e:
        return _err(str(e))

    body = {
        "session_valid": valid,
        "session_locked": locked,
        "idle_hours": round(idle, 1) if idle is not None else None,
        "final_url": final_url,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if valid:
        return _ok({**body, "message": "세션 유지 — 유휴 타이머 리셋"})
    # 잠금이어도 무인 재로그인은 하지 않는다 — SSO 가 사람을 요구하고, 30분마다 도는
    # 백그라운드 작업이 창을 띄우면 화면을 점거한다. 원인만 정확히 알린다.
    return {**body, "success": False,
            "error": (_LOCK_HINT if locked
                      else "세션 만료 — klms_login 으로 SSO 로그인이 필요합니다 (무인 갱신 불가)")}


def check_session() -> dict:
    """
    저장된 쿠키로 KLMS 세션이 유효한지 확인합니다.

    Returns:
        {"success": True, "session_valid": bool, "age_hours": float|None, "checked_at": str}
    """
    try:
        age = session_age_hours()
        idle = session_idle_hours()  # 요청을 보내면 갱신되므로 먼저 읽는다
        session = _make_session()
        r = session.get(f"{KLMS_BASE}/my/", allow_redirects=True, timeout=HTTP_TIMEOUT)
        valid = _is_session_valid(r.url, r.text, require_dashboard=True)
        locked = _is_session_locked(r.text)

        if locked:
            message = _LOCK_HINT
        elif not valid:
            message = "세션 만료 — login() 실행 필요"
        elif idle is not None and idle >= _IDLE_WARN_HOURS:
            # 만료를 맞고 나서 알면 이미 조회가 실패한 뒤다. 미리 알린다.
            message = (f"세션 유효하지만 {idle:.0f}시간 유휴 — keepalive 가 도는지 "
                       f"확인하세요 (./launchd.sh status)")
        else:
            message = "세션 유효"

        return _ok({
            "session_valid": valid,
            "session_locked": locked,
            "age_hours": round(age, 1) if age is not None else None,
            "idle_hours": round(idle, 1) if idle is not None else None,
            "final_url": r.url,
            "checked_at": datetime.now(tz=timezone.utc).isoformat(),
            "message": message,
        })
    except FileNotFoundError:
        return _err("cookies.json 없음 — login() 을 먼저 실행하세요.")
    except Exception as e:
        return _err(str(e))


# ── 공개 API: 과목 ────────────────────────────────────────────────────────

def get_courses() -> dict:
    """
    현재 수강 중인 과목 목록을 가져옵니다.

    Returns:
        {"success": True, "courses": [{"name": str, "id": str, "url": str}]}
    """
    try:
        session = _make_session()
        r = session.get(f"{KLMS_BASE}/my/", timeout=HTTP_TIMEOUT)
        # 이 가드가 없으면 만료된 세션이 "수강 과목 없음"으로 보고된다.
        # get_course_activities / list_course_files / download_course_files 가
        # 모두 이 함수로 과목을 찾으므로, 여기 한 곳이 전체 경로를 커버한다.
        if not _is_session_valid(r.url, r.text, require_dashboard=True):
            return _err("KLMS 세션 만료 — login() 을 실행해 쿠키를 갱신하세요.")
        soup = BeautifulSoup(r.text, "html.parser")

        links = soup.select("ul.main-course-list.student a[href*='/course/view.php?id=']")
        if not links:
            links = soup.select("a[href*='/course/view.php?id=']")

        courses = []
        seen: set[str] = set()
        for link in links:
            href = _norm_url(link.get("href", ""))
            parsed = urlparse(href)
            params = {k: v for k, v in (p.split("=", 1) for p in parsed.query.split("&") if "=" in p)}
            if "id" not in params or params["id"] in seen:
                continue
            seen.add(params["id"])
            url = urlunparse(parsed._replace(query=f"id={params['id']}"))
            courses.append({"name": link.get_text(" ", strip=True), "id": params["id"], "url": url})

        return _ok({"courses": courses, "count": len(courses)})
    except Exception as e:
        return _err(str(e))


def get_course_activities(course_name: str) -> dict:
    """
    특정 과목의 활동 목록(과제, 퀴즈, 게시판, 파일 등)을 가져옵니다.

    Args:
        course_name: 과목명 일부 (예: '데이타구조', '선형대수학')

    Returns:
        {"success": True, "course": str, "activities": [{"type": str, "name": str, "url": str}]}
    """
    try:
        courses_result = get_courses()
        if not courses_result["success"]:
            return courses_result

        matched = [c for c in courses_result["courses"] if course_name in c["name"]]
        if not matched:
            return _err(f"'{course_name}' 과목을 찾지 못했습니다. 수강 과목: {[c['name'] for c in courses_result['courses']]}")

        course = matched[0]
        session = _make_session()
        r = session.get(course["url"], timeout=HTTP_TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")

        activities = []
        for li in soup.select("li.activity"):
            name_el = li.select_one(".instancename, .activitytitle, .activity-name")
            if not name_el:
                continue
            for hidden in name_el.select(".accesshide"):
                hidden.decompose()
            link = li.select_one("a[href*='/mod/']") or li.select_one("a[href]")
            entry = {}
            if link:
                href = _norm_url(link.get("href", ""))
                m = re.search(r"/mod/(\w+)/", href)
                mtype = m.group(1) if m else "unknown"
            else:
                # 접근 제한(오픈 전) 활동은 링크 없이 이름만 렌더된다.
                # 건너뛰면 '과제가 없다'와 구별되지 않으므로 module id 로 URL 을 만든다.
                mid = re.match(r"module-(\d+)$", li.get("id") or "")
                mtype = next((c[len("modtype_"):] for c in li.get("class", [])
                              if c.startswith("modtype_")), "")
                if not mid or not mtype:
                    continue
                href = f"{KLMS_BASE}/mod/{mtype}/view.php?id={mid.group(1)}"
            avail = li.select_one(".availabilityinfo.isrestricted")
            if avail:
                entry["restricted"] = True
                entry["availability"] = avail.get_text(" ", strip=True)
            period = li.select_one(".end-dt")
            if period:
                entry["period"] = period.get_text(" ", strip=True)
            activities.append({
                "type": mtype,
                "name": name_el.get_text(" ", strip=True),
                "url": href,
                **entry,
            })

        return _ok({"course": course["name"], "activities": activities, "count": len(activities)})
    except Exception as e:
        return _err(str(e))


# ── 공개 API: 알림 ────────────────────────────────────────────────────────

def get_notifications(limit: int = 20) -> dict:
    """
    Moodle 알림 API로 최근 알림 목록을 가져옵니다.
    (과제 피드백, 새 공지, 성적 등 Moodle 이벤트 전체)

    Args:
        limit: 가져올 알림 수 (최대 50 권장)

    Returns:
        {"success": True, "notifications": [...], "unread_count": int}
    """
    try:
        session = _make_session()
        sesskey = _get_sesskey(session)
        if not sesskey:
            return _err("sesskey 추출 실패 — 세션이 만료됐을 수 있습니다.")

        resp = session.post(
            f"{KLMS_BASE}/lib/ajax/service.php",
            params={"sesskey": sesskey, "info": "message_popup_get_popup_notifications"},
            json=[{
                "index": 0,
                "methodname": "message_popup_get_popup_notifications",
                "args": {"useridto": 0, "newestfirst": True, "limit": limit, "offset": 0},
            }],
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data or data[0].get("error"):
            return _err(f"알림 API 오류: {data[0].get('error') if data else '응답 없음'}")

        raw_notifs = data[0].get("data", {}).get("notifications", [])
        unread_count = data[0].get("data", {}).get("unreadcount", 0)

        notifications = []
        for n in raw_notifs:
            full = n.get("fullmessage", "")
            first_line = full.split("\n")[0].strip()
            parts = [p.strip() for p in first_line.split("->")]
            notifications.append({
                "id": n.get("id"),
                "subject": n.get("subject", ""),
                "course": parts[0] if parts else "",
                "category": parts[1] if len(parts) > 1 else "",
                "url": n.get("contexturl", ""),
                "received_at": datetime.fromtimestamp(
                    n.get("timecreated", 0), tz=timezone.utc
                ).isoformat() if n.get("timecreated") else "",
                "read": n.get("read", False),
            })

        return _ok({"notifications": notifications, "unread_count": unread_count, "count": len(notifications)})
    except Exception as e:
        return _err(str(e))


def get_new_notifications() -> dict:
    """
    마지막 확인 이후 새로운 알림만 가져옵니다.
    상태를 state/notification_state.json에 저장해 중복 방지.

    Returns:
        {"success": True, "new_notifications": [...], "new_count": int}
    """
    result = get_notifications(limit=50)
    if not result["success"]:
        return result

    state = _load_state("notification")
    last_seen_id = state.get("last_seen_id", 0)

    new = [n for n in result["notifications"] if n.get("id", 0) > last_seen_id]

    if result["notifications"]:
        max_id = max(n["id"] for n in result["notifications"])
        state["last_seen_id"] = max_id
    state["checked_at"] = datetime.now(tz=timezone.utc).isoformat()
    _save_state("notification", state)

    return _ok({
        "new_notifications": new,
        "new_count": len(new),
        "unread_count": result["unread_count"],
        "message": f"새 알림 {len(new)}개" if new else "새 알림 없음",
    })


# ── 공개 API: 과목별 공지 ─────────────────────────────────────────────────

_NOTICE_SLUG_MAX = 40


def _notice_body(main) -> str:
    """공지 본문을 통째로 뽑는다. 자르지 않는다.

    예전엔 300자에서 잘라 반환값에만 실었다. 어디에도 원문이 남지 않아 잘린
    뒤쪽은 사라졌다 — MAS355 'Course Info.'(2026-09-02)가 '[TA Info.] - Hea'
    에서 잘려 학기 전체 퀴즈 일정표와 조교 연락처가 통째로 유실됐다.
    """
    el = main.select_one("div.content div.text_to_html") or main.select_one("div.content")
    return el.get_text(" ", strip=True) if el else ""


_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "table", "tr", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


def _notice_content(main) -> dict:
    """공지 한 건의 모든 내용 — 링크 주소·본문 이미지·첨부·원본 HTML 까지.

    _notice_body() 는 일정 파싱용 평문이다. get_text() 라 링크는 글자만,
    이미지는 아예 사라진다. 실제로 EE202 'HW1/HW2 Graded & Returned'
    (2026-09-29, 10-02) 공지의 성적 분포 스크린샷이 그렇게 유실됐다.
    노트 파일에는 이쪽(markdown)을 쓴다.

    Returns: {"markdown", "images", "attachments", "html"}
    """
    from bs4 import Comment, NavigableString

    el = main.select_one("div.content div.text_to_html") or main.select_one("div.content")
    images: list[str] = []
    parts: list[str] = []

    def walk(node):
        for ch in node.children:
            if isinstance(ch, Comment):
                continue
            if isinstance(ch, NavigableString):
                parts.append(re.sub(r"\s+", " ", str(ch)))
            elif ch.name in ("script", "style"):
                continue
            elif ch.name == "br":
                parts.append("\n")
            elif ch.name == "img":
                src = _norm_url(ch.get("src", ""))
                if src:
                    if src not in images:
                        images.append(src)
                    parts.append(f"![{ch.get('alt', '')}]({src})")
            elif ch.name == "a" and _norm_url(ch.get("href", "")) and not ch.find("img"):
                href = _norm_url(ch["href"])
                text = ch.get_text(" ", strip=True)
                parts.append(f"[{text}]({href})" if text and text != href else f"<{href}>")
            else:
                block = ch.name in _BLOCK_TAGS
                if block:
                    parts.append("\n")
                if ch.name == "li":
                    parts.append("- ")
                walk(ch)
                if block:
                    parts.append("\n")

    if el:
        walk(el)
    md = "".join(parts)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n[ \t]+", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()

    attachments: list[str] = []
    files_el = main.select_one("div.files")
    if files_el:
        for a in files_el.find_all("a", href=True):
            href = _norm_url(a["href"])
            if "pluginfile" in href and href not in attachments:
                attachments.append(href)

    html = "".join(str(x) for x in (el, files_el) if x is not None)
    return {"markdown": md, "images": images, "attachments": attachments, "html": html}


def _asset_name(url: str) -> str:
    """pluginfile URL → 저장 파일명. 경로 구분자는 걷어낸다."""
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "file"
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def _save_notice_assets(notice: dict, asset_dir: Path, session) -> dict:
    """본문 이미지·첨부·원본 HTML 을 asset_dir 에 받는다. 이미 있으면 건너뛴다.

    Returns: {원문 URL: 저장된 파일명}
    """
    saved: dict[str, str] = {}
    urls = list(notice.get("images") or []) + list(notice.get("attachments") or [])
    html = notice.get("html") or ""
    if not urls and not html:
        return saved
    asset_dir.mkdir(parents=True, exist_ok=True)
    if html:
        (asset_dir / "original.html").write_text(html, encoding="utf-8")
    if session is None:
        return saved
    used: set[str] = set()
    for url in urls:
        name = _asset_name(url)
        stem, suffix = os.path.splitext(name)
        i = 1
        while name in used:
            i += 1
            name = f"{stem}_{i}{suffix}"
        used.add(name)
        dest = asset_dir / name
        if not dest.is_file():
            try:
                r = session.get(url, timeout=HTTP_TIMEOUT)
            except Exception:
                continue
            if getattr(r, "status_code", 200) != 200:
                continue
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(r.content)
            tmp.replace(dest)
        saved[url] = name
    return saved


def _notice_slug(title: str) -> str:
    """공지 제목을 파일명 조각으로. 경로 구분자와 셸 특수문자를 걷어낸다."""
    s = re.sub(r'[\\/:*?"<>|#\[\]]', " ", title or "")
    s = re.sub(r"[\s.]+", "-", s.strip())
    return s.strip("-")[:_NOTICE_SLUG_MAX].strip("-") or "notice"


_NOTICE_MARKER = "<!-- klms-notice: 자동 저장본 -->"


def _existing_notice_file(out_dir: Path, url: str):
    """이 공지의 원문 URL 을 이미 가리키는 파일을 찾는다.

    파일명으로 찾으면 안 된다. 사람이 정리해둔 노트는 제목과 다른 이름을 쓰고
    (`notice_2026-09-04_recitation-problems.md`), macOS 파일시스템은 대소문자를
    구분하지 않아 `_hw1.md` 와 `_HW1.md` 가 같은 파일이다. 실제로 손으로 써둔
    노트를 자동 저장이 통째로 덮어쓴 적이 있다.

    Returns: (경로, 자동저장본인가) 또는 (None, False)
    """
    if not url or not out_dir.is_dir():
        return None, False
    for path in sorted(out_dir.glob("*.md")):
        try:
            text = _read_text(path)
        except OSError:
            continue
        # 'id=123' 이 'id=1234' 안에 들어 있다고 같은 공지로 치면 안 된다.
        if re.search(re.escape(url) + r"(?![0-9])", text):
            return path, _NOTICE_MARKER in text
    return None, False


def _save_notice(notice: dict, base, session=None) -> Optional[str]:
    """공지 전문을 {과목}/notices/ 에 남기고 그 경로를 돌려준다.

    KLMS 에서 글이 지워질 수 있고, 학기 중에 로컬에서 찾아볼 수 있어야 한다.
    본문(링크 주소 포함)·본문 이미지·첨부·원본 HTML 을 모두 남긴다 —
    이미지·첨부·원본은 notices/files/{노트 이름}/ 에 받는다 (session 이 있을 때).
    사람이 손으로 정리해둔 노트가 이미 그 공지를 담고 있으면 글은 손대지 않는다 —
    첨부 링크나 메모가 붙어 있어 자동 저장본보다 내용이 많다. 자산만 옆에 받는다.
    """
    body = (notice.get("markdown") or notice.get("body") or "").strip()
    if not body and not notice.get("images") and not notice.get("attachments"):
        return None
    out_dir = _course_dest_dir(notice.get("course", ""), Path(base)) / "notices"
    url = notice.get("url", "")

    existing, is_ours = _existing_notice_file(out_dir, url)
    if existing is not None and not is_ours:
        # 사람 손 노트 — 글은 그대로 두고 이미지·첨부만 옆에 받는다
        _save_notice_assets(notice, out_dir / "files" / existing.stem, session)
        return str(existing)

    date = notice.get("posted_at") or "undated"
    path = existing or out_dir / f"notice_{date}_{_notice_slug(notice.get('title', ''))}.md"

    out_dir.mkdir(parents=True, exist_ok=True)
    local = _save_notice_assets(notice, out_dir / "files" / path.stem, session)

    def rel(u: str) -> str:
        return f"files/{path.stem}/{quote(local[u])}"

    for img in notice.get("images") or []:
        if img in local:
            body = body.replace(f"]({img})", f"]({rel(img)})")
    extra = ""
    if notice.get("images"):
        extra += "\n\n## 본문 이미지\n\n" + "\n".join(
            f"- [{local.get(u, _asset_name(u))}]({rel(u) if u in local else u}) — 원문: {u}"
            for u in notice["images"])
    if notice.get("attachments"):
        extra += "\n\n## 첨부\n\n" + "\n".join(
            f"- [{local.get(u, _asset_name(u))}]({rel(u) if u in local else u}) — 원문: {u}"
            for u in notice["attachments"])
    body = body + extra

    text = (
        f"{_NOTICE_MARKER}\n"
        f"# {notice.get('title', '').strip()}\n\n"
        f"- 과목: {notice.get('course', '')}\n"
        f"- 게시판: {notice.get('board', '')}\n"
        f"- 게시일: {date}\n"
        f"- 원문: {url}\n\n"
        "---\n\n"
        f"{body}\n"
    )
    if path.is_file() and _read_text(path) == text:
        return str(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def get_course_notices(
    course_filter: Optional[str] = None,
    include_seen: bool = False,
    dest_dir: Optional[str] = None,
) -> dict:
    """
    각 수강 과목의 공지(Announcements/Notice) 게시판 글을 가져옵니다.
    기본은 새 글만 — 이미 확인한 글은 state/notice_state.json으로 추적해 중복 방지.

    Args:
        course_filter: 특정 과목명 일부 (None이면 전체)
        dest_dir:      공지 전문을 남길 경로 (None이면 과목별 기본 경로)
        include_seen:  True 면 이미 본 공지도 전부 반환한다. 기준선 시딩으로
                       한 번도 본문을 받지 않은 개강 초 공지들을 되찾을 때 쓴다.
                       state 는 그대로 갱신하되 아무것도 걸러내지 않는다.

    Returns:
        {"success": True, "new_notices": [...], "new_count": int}
    """
    try:
        session = _make_session()

        # 대시보드
        r = session.get(f"{KLMS_BASE}/my/", timeout=HTTP_TIMEOUT)
        if not _is_session_valid(r.url, r.text):
            return _err("세션 만료 — login() 실행 필요")

        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.select("ul.main-course-list.student a[href*='/course/view.php?id=']") or \
                soup.select("a[href*='/course/view.php?id=']")

        courses = []
        seen: set[str] = set()
        for link in links:
            href = _norm_url(link.get("href", ""))
            parsed = urlparse(href)
            params = {k: v for k, v in (p.split("=", 1) for p in parsed.query.split("&") if "=" in p)}
            if "id" not in params or params["id"] in seen:
                continue
            seen.add(params["id"])
            name = link.get_text(" ", strip=True)
            if course_filter and course_filter not in name:
                continue
            courses.append((name, urlunparse(parsed._replace(query=f"id={params['id']}"))))

        save_dir = Path(dest_dir) if dest_dir else _get_download_dir()
        state = _load_state("notice")
        known_urls: set[str] = set(state.get("known_urls", []))
        # 이미 한 번 훑은 과목. 새 학기에 과목이 추가되면 그 과목만 다시 시딩된다.
        seeded: set[str] = set(state.get("seeded_courses", []))

        new_notices = []
        for course_name, course_url in courses:
            # 처음 보는 과목은 기존 공지를 전부 '새 공지'로 쏟아내면 안 된다.
            # URL 만 기록해 기준선을 잡고, 다음 실행부터 진짜 새 글만 보고한다.
            first_visit = course_name not in seeded and not include_seen

            time.sleep(FETCH_DELAY)
            cr = session.get(course_url, timeout=HTTP_TIMEOUT)
            csoup = BeautifulSoup(cr.text, "html.parser")

            # 공지 게시판 찾기
            for li in csoup.select("li.activity"):
                ba = li.select_one("a[href*='mod/courseboard']")
                if not ba:
                    continue
                href = _norm_url(ba.get("href", ""))
                if "view.php" not in href:
                    continue
                name_el = li.select_one(".instancename, .activitytitle, .activity-name")
                board_name = name_el.get_text(" ", strip=True) if name_el else ""
                if not any(kw in board_name.lower() for kw in NOTICE_BOARD_KEYWORDS):
                    continue

                time.sleep(FETCH_DELAY)
                br = session.get(href, timeout=HTTP_TIMEOUT)
                bsoup = BeautifulSoup(br.text, "html.parser")

                for a in bsoup.find_all("a", href=True):
                    ahref = _norm_url(a.get("href", ""))
                    if "mod/courseboard/article.php" not in ahref:
                        continue
                    # 이미 아는 글이나 처음 보는 과목의 기존 글은 '새 공지'로 알리지 않는다.
                    # 그래도 저장 위치에 파일이 없으면 받아 둔다. 처음 쓰는 사람도
                    # '중간고사 통계 공지 읽어줘' 같은 질문에 답할 수 있어야 한다.
                    quiet = (first_visit or ahref in known_urls) and not include_seen
                    if quiet and _existing_notice_file(
                            _course_dest_dir(course_name, save_dir) / "notices", ahref)[0]:
                        known_urls.add(ahref)
                        continue

                    time.sleep(FETCH_DELAY)
                    ar = session.get(ahref, timeout=HTTP_TIMEOUT)
                    asoup = BeautifulSoup(ar.text, "html.parser")
                    main = asoup.select_one("div[role=main]") or asoup

                    subject_el = main.select_one("div.subject h3")
                    title = subject_el.get_text(" ", strip=True) if subject_el else ar.url

                    posted_at = ""
                    for date_div in main.select("div.date"):
                        lbl = date_div.select_one("span.title")
                        if lbl and ("작성일" in lbl.get_text() or "Wrote" in lbl.get_text()):
                            iso = _parse_kr_datetime(date_div.get_text(" ", strip=True))
                            dm = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", date_div.get_text())
                            if iso:
                                posted_at = iso[:10]
                            elif dm:
                                posted_at = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
                            break

                    notice = {
                        "course": course_name,
                        "board": board_name,
                        "title": title,
                        "posted_at": posted_at,
                        "body": _notice_body(main),
                        "url": ahref,
                        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                    }
                    content = _notice_content(main)
                    saved = _save_notice({**notice, **content}, save_dir, session=session)
                    notice["images"] = content["images"]
                    notice["attachments"] = content["attachments"]
                    if saved:
                        notice["local_path"] = saved
                    if not quiet:
                        new_notices.append(notice)
                    known_urls.add(ahref)

            # 과목 단위로 저장한다. 마지막에 한 번만 저장하면 네트워크가 한 번 끊길 때
            # 이미 받아온 과목들의 진행까지 통째로 버려진다.
            seeded.add(course_name)
            state["known_urls"] = list(known_urls)
            state["seeded_courses"] = list(seeded)
            state["checked_at"] = datetime.now(tz=timezone.utc).isoformat()
            _save_state("notice", state)

        label = "공지" if include_seen else "새 공지"
        return _ok({
            "new_notices": new_notices,
            "new_count": len(new_notices),
            "include_seen": include_seen,
            "message": f"{label} {len(new_notices)}개" if new_notices else f"{label} 없음",
        })
    except Exception as e:
        return _err(str(e))


# ── 공개 API: Moodle Web Service (빠른 경로) ────────────────────────────

def get_course_contents_api(course_id: str) -> dict:
    """
    Moodle core_course_get_contents API로 과목 전체 콘텐츠를 1회 호출로 조회.
    크롤링 대비 10배+ 빠름.

    Args:
        course_id: KLMS 과목 ID (예: '184848')

    Returns:
        {"success": True, "files": [...], "sections": [...]}
    """
    try:
        session = _make_session()
        sesskey = _get_sesskey(session)
        if not sesskey:
            return _err("sesskey 추출 실패 — 세션 만료일 수 있습니다")

        resp = session.post(
            f"{KLMS_BASE}/lib/ajax/service.php",
            params={"sesskey": sesskey, "info": "core_course_get_contents"},
            json=[{
                "index": 0,
                "methodname": "core_course_get_contents",
                "args": {"courseid": int(course_id)},
            }],
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()

        if not payload or payload[0].get("error"):
            exc = payload[0].get("exception", {}) if payload else {}
            return _err(exc.get("message", "core_course_get_contents 오류"))

        raw = payload[0].get("data", payload[0])
        if not isinstance(raw, list):
            return _err(f"예상과 다른 응답: {type(raw).__name__}")

        files = []
        for section in raw:
            sec_name = section.get("name", "")
            for mod in section.get("modules", []):
                for c in mod.get("contents", []):
                    if c.get("type") != "file":
                        continue
                    files.append({
                        "section": sec_name,
                        "module": mod.get("name", ""),
                        "modtype": mod.get("modname", ""),
                        "filename": c.get("filename", ""),
                        "fileurl": c.get("fileurl", ""),
                        "filesize": c.get("filesize", 0),
                        "timemodified": c.get("timemodified", 0),
                    })

        return _ok({
            "course_id": str(course_id),
            "sections": [
                {"name": s.get("name", ""), "module_count": len(s.get("modules", []))}
                for s in raw
            ],
            "files": files,
            "file_count": len(files),
        })
    except Exception as e:
        return _err(str(e))


def _list_files_for_course(course: dict, session: requests.Session) -> list[dict]:
    """한 과목의 파일 목록을 크롤링으로 수집 (병렬 호출용)."""
    files = []
    cid = course["id"]
    cname = course["name"]

    # resource index
    r = session.get(f"{KLMS_BASE}/mod/resource/index.php?id={cid}", timeout=HTTP_TIMEOUT)
    soup = BeautifulSoup(r.text, "html.parser")
    main = soup.select_one("div[role=main]") or soup
    for tr in main.select("tr"):
        a = tr.select_one("a[href*='view.php']")
        if not a:
            continue
        href = a.get("href", "")
        if not href.startswith("http"):
            href = f"{KLMS_BASE}/mod/resource/{href.lstrip('./')}"
        files.append({"course": cname, "name": a.get_text(strip=True), "view_url": href, "source": "resource"})

    # course page — folders + courseboard
    time.sleep(FETCH_DELAY)
    cr = session.get(course["url"], timeout=HTTP_TIMEOUT)
    csoup = BeautifulSoup(cr.text, "html.parser")
    for li in csoup.select("li.activity"):
        fa = li.select_one("a[href*='/mod/folder/']")
        if fa:
            time.sleep(FETCH_DELAY)
            fr = session.get(_norm_url(fa.get("href", "")), timeout=HTTP_TIMEOUT)
            fsoup = BeautifulSoup(fr.text, "html.parser")
            fmain = fsoup.select_one("div[role=main]") or fsoup
            for pa in fmain.find_all("a", href=re.compile(r"pluginfile")):
                url = pa.get("href", "")
                filename = unquote(url.split("/")[-1].split("?")[0])
                files.append({"course": cname, "name": filename, "view_url": url, "source": "folder", "file_url": url})
            continue

        ba = li.select_one("a[href*='mod/courseboard']")
        if not ba:
            continue
        bhref = _norm_url(ba.get("href", ""))
        if "view.php" not in bhref:
            continue
        name_el = li.select_one(".instancename, .activitytitle, .activity-name")
        board_name = name_el.get_text(" ", strip=True) if name_el else ""

        time.sleep(FETCH_DELAY)
        br = session.get(bhref, timeout=HTTP_TIMEOUT)
        bsoup = BeautifulSoup(br.text, "html.parser")
        for aa in bsoup.find_all("a", href=True):
            ahref = _norm_url(aa.get("href", ""))
            if "mod/courseboard/article.php" not in ahref:
                continue
            time.sleep(FETCH_DELAY)
            ar = session.get(ahref, timeout=HTTP_TIMEOUT)
            asoup = BeautifulSoup(ar.text, "html.parser")
            amain = asoup.select_one("div[role=main]") or asoup
            subject_el = amain.select_one("div.subject h3")
            post_title = subject_el.get_text(" ", strip=True) if subject_el else ""
            for pa in amain.find_all("a", href=re.compile(r"pluginfile")):
                url = pa.get("href", "")
                filename = unquote(url.split("/")[-1].split("?")[0])
                files.append({
                    "course": cname, "name": filename, "view_url": url,
                    "source": "courseboard", "board": board_name,
                    "post_title": post_title, "file_url": url,
                })
    return files


def _gather_course_files(courses, worker, retries: int = COURSE_LIST_RETRIES):
    """과목별 파일 목록을 병렬 수집하고, 실패한 과목만 다시 시도한다.

    한 과목이 타임아웃 났다고 그 회차의 자료를 잃으면 안 된다. 성공한 과목을
    다시 긁는 것도 낭비라, 재시도 대상은 실패한 과목으로만 좁힌다.

    Returns: (files, errors)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    files: list[dict] = []
    pending = list(courses)
    last_error: dict[str, str] = {}

    for attempt in range(retries + 1):
        if not pending:
            break
        failed = []
        with ThreadPoolExecutor(max_workers=min(len(pending), 4)) as pool:
            futs = {pool.submit(worker, c): c for c in pending}
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    files.extend(fut.result())
                    last_error.pop(c["name"], None)
                except Exception as e:
                    last_error[c["name"]] = str(e)
                    failed.append(c)
        pending = failed

    errors = [f"{name}: {err}" for name, err in last_error.items()]
    return files, errors


def download_new_files_fast(
    course_filter: Optional[str] = None,
    since_hours: Optional[int] = None,
    dest_dir: Optional[str] = None,
) -> dict:
    """
    과목별 병렬 크롤링으로 새 파일을 빠르게 다운로드.
    ThreadPoolExecutor로 과목을 동시 처리해 4과목 기준 ~4배 빠름.

    Args:
        course_filter: 과목명 일부 (None이면 전체)
        since_hours:   사용하지 않음 (state 기반 중복 방지)
        dest_dir:      저장 경로

    Returns:
        {"success": True, "downloaded": [...], "downloaded_count": int}
    """
    try:
        courses_result = get_courses()
        if not courses_result["success"]:
            return courses_result

        courses = courses_result["courses"]
        if course_filter:
            courses = [c for c in courses if course_filter in c["name"]]
        if not courses:
            return _err(f"'{course_filter}' 과목 없음")

        save_dir = Path(dest_dir) if dest_dir else _get_download_dir()
        state = _load_state("file")
        already: set[str] = set(state.get("downloaded_urls", []))
        fingerprints: dict = dict(state.get("fingerprints", {}))
        # 원격 파일명 → 사용자가 이름을 바꿔 정리해둔 로컬 경로.
        # 내용 대조로 알아낸 결과를 기억해두지 않으면 다음 회차에도 '로컬에
        # 없는 파일'로 보여 매번 다시 받는다.
        aliases: dict = dict(state.get("aliases", {}))

        # 과목별 병렬 파일 목록 수집 (실패한 과목은 재시도)
        def _worker(course):
            sess = _make_session()
            return _list_files_for_course(course, sess)

        all_files, errors = _gather_course_files(courses, _worker)

        # 다운로드
        session = _make_session()
        downloaded, updated, failed = [], [], []

        for f in all_files:
            file_url = f.get("file_url", "")
            if not file_url:
                r = session.get(f["view_url"], allow_redirects=False, timeout=HTTP_TIMEOUT)
                file_url = r.headers.get("Location", "")
                if not file_url:
                    soup = BeautifulSoup(r.text, "html.parser")
                    pa = soup.select_one("a[href*='pluginfile']")
                    file_url = pa.get("href", "") if pa else ""
            if not file_url:
                continue

            filename = unquote(file_url.split("/")[-1].split("?")[0])

            course_dir = _course_dest_dir(f["course"], save_dir)
            dest = course_dir / _dest_subdir(filename) / filename
            fp_key = _resource_key(f, filename)
            if fp_key not in fingerprints and f"{f['course']}/{filename}" in fingerprints:
                # 파일명 기반 키로 저장된 예전 기록을 물려받는다 (한 번만 일어난다).
                legacy = f"{f['course']}/{filename}"
                fingerprints[fp_key] = fingerprints.pop(legacy)
                if legacy in aliases:
                    aliases[fp_key] = aliases.pop(legacy)
            if not dest.is_file():
                # lectures/ 등으로 분류해 넣어둔 파일이 정본이다. 갱신도 그 자리에.
                aliased = aliases.get(fp_key, "")
                dest = (
                    _find_local_copy(course_dir, filename)
                    or (Path(aliased) if aliased and Path(aliased).is_file() else dest)
                )
            local_exists = dest.is_file()
            local_size = dest.stat().st_size if local_exists else 0

            # 파일명이 같아도 내용이 바뀌었을 수 있다. 원격 지문을 먼저 본다.
            remote_fp = _remote_fingerprint(session, file_url)
            want, reason = _should_download(
                local_exists, local_size, fingerprints.get(fp_key), remote_fp
            )
            if not want:
                already.add(file_url)
                if reason == "adopted":
                    # 지문을 남겨둬야 다음번에 변경을 알아챌 수 있다.
                    fingerprints[fp_key] = remote_fp
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            superseded = _supersede(dest) if local_exists else None

            try:
                dr = session.get(file_url, allow_redirects=True, timeout=60, stream=True)
                if dr.status_code != 200 or "text/html" in dr.headers.get("Content-Type", ""):
                    failed.append({"name": filename, "course": f["course"], "error": f"HTTP {dr.status_code}"})
                    if superseded:  # 받기 실패했으면 원래 파일을 되돌린다.
                        superseded.replace(dest)
                    continue
                size = 0
                with open(dest, "wb") as fh:
                    for chunk in dr.iter_content(65536):
                        fh.write(chunk)
                        size += len(chunk)
                already.add(file_url)
                fingerprints[fp_key] = remote_fp
                entry = {
                    "course": f["course"],
                    "filename": filename,
                    "size_bytes": size,
                    "saved_to": str(dest),
                    "reason": reason,
                }
                if superseded and _discard_if_identical(superseded, dest):
                    # 내용은 그대로였다. 지문만 갱신하고 조용히 넘어간다.
                    continue
                if not superseded:
                    twin = _dedupe_against_dir(dest, course_dir)
                    if twin is not None:
                        # 사용자가 이름만 바꿔둔 같은 자료였다. 그쪽을 정본으로
                        # 삼고, 다음 회차엔 대조 없이 알아보도록 적어둔다.
                        aliases[fp_key] = str(twin)
                        continue
                if superseded:
                    entry["previous_version"] = str(superseded)
                    updated.append(entry)
                else:
                    downloaded.append(entry)
            except Exception as ex:
                failed.append({"name": filename, "course": f["course"], "error": str(ex)})
                if superseded:
                    superseded.replace(dest)

        state["downloaded_urls"] = list(already)
        state["fingerprints"] = fingerprints
        state["aliases"] = aliases
        state["last_run"] = datetime.now(tz=timezone.utc).isoformat()
        _save_state("file", state)

        return _ok({
            "downloaded": downloaded,
            "downloaded_count": len(downloaded),
            "updated": updated,
            "updated_count": len(updated),
            "failed": failed,
            "failed_count": len(failed),
            "errors": errors,
            "message": (
                f"{len(downloaded)}개 다운로드"
                + (f", {len(updated)}개 갱신" if updated else "")
                + (f", {len(failed)}개 실패" if failed else "")
                + (f", 오류: {errors}" if errors else "")
            ),
        })
    except Exception as e:
        return _err(str(e))


# ── 공개 API: 파일 (크롤링 방식, 레거시) ─────────────────────────────────

def list_course_files(course_filter: Optional[str] = None) -> dict:
    """
    수강 과목에 업로드된 파일 목록을 가져옵니다.
    (mod/resource 단독 파일 + mod/folder 내 파일 + 게시판 첨부파일)

    Args:
        course_filter: 특정 과목명 일부 (None이면 전체)

    Returns:
        {"success": True, "files": [{"course": str, "name": str, "week": str, "view_url": str}]}
    """
    try:
        courses_result = get_courses()
        if not courses_result["success"]:
            return courses_result

        courses = courses_result["courses"]
        if course_filter:
            courses = [c for c in courses if course_filter in c["name"]]

        session = _make_session()
        all_files = []

        for course in courses:
            time.sleep(FETCH_DELAY)
            # resource index
            r = session.get(f"{KLMS_BASE}/mod/resource/index.php?id={course['id']}", timeout=HTTP_TIMEOUT)
            soup = BeautifulSoup(r.text, "html.parser")
            main = soup.select_one("div[role=main]") or soup

            for tr in main.select("tr"):
                a = tr.select_one("a[href*='view.php']")
                if not a:
                    continue
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = f"{KLMS_BASE}/mod/resource/{href.lstrip('./')}"
                cells = [td.get_text(" ", strip=True) for td in tr.select("td")]
                all_files.append({
                    "course": course["name"],
                    "name": a.get_text(strip=True),
                    "week": cells[1] if len(cells) > 1 else "",
                    "view_url": href,
                    "source": "resource",
                })

            # folder modules + courseboard attachments
            time.sleep(FETCH_DELAY)
            cr = session.get(course["url"], timeout=HTTP_TIMEOUT)
            csoup = BeautifulSoup(cr.text, "html.parser")
            for li in csoup.select("li.activity"):
                # folder
                fa = li.select_one("a[href*='/mod/folder/']")
                if fa:
                    time.sleep(FETCH_DELAY)
                    fr = session.get(_norm_url(fa.get("href", "")), timeout=HTTP_TIMEOUT)
                    fsoup = BeautifulSoup(fr.text, "html.parser")
                    fmain = fsoup.select_one("div[role=main]") or fsoup
                    for pa in fmain.find_all("a", href=re.compile(r"pluginfile")):
                        url = pa.get("href", "")
                        filename = unquote(url.split("/")[-1].split("?")[0])
                        all_files.append({
                            "course": course["name"],
                            "name": pa.get_text(strip=True) or filename,
                            "week": "",
                            "view_url": url,
                            "source": "folder",
                            "file_url": url,
                        })
                    continue

                # courseboard (게시판 첨부파일)
                ba = li.select_one("a[href*='mod/courseboard']")
                if not ba:
                    continue
                bhref = _norm_url(ba.get("href", ""))
                if "view.php" not in bhref:
                    continue
                name_el = li.select_one(".instancename, .activitytitle, .activity-name")
                board_name = name_el.get_text(" ", strip=True) if name_el else ""

                time.sleep(FETCH_DELAY)
                br = session.get(bhref, timeout=HTTP_TIMEOUT)
                bsoup = BeautifulSoup(br.text, "html.parser")

                for aa in bsoup.find_all("a", href=True):
                    ahref = _norm_url(aa.get("href", ""))
                    if "mod/courseboard/article.php" not in ahref:
                        continue

                    time.sleep(FETCH_DELAY)
                    ar = session.get(ahref, timeout=HTTP_TIMEOUT)
                    asoup = BeautifulSoup(ar.text, "html.parser")
                    amain = asoup.select_one("div[role=main]") or asoup

                    subject_el = amain.select_one("div.subject h3")
                    post_title = subject_el.get_text(" ", strip=True) if subject_el else ""

                    for pa in amain.find_all("a", href=re.compile(r"pluginfile")):
                        url = pa.get("href", "")
                        filename = unquote(url.split("/")[-1].split("?")[0])
                        all_files.append({
                            "course": course["name"],
                            "name": filename,
                            "week": "",
                            "view_url": url,
                            "source": "courseboard",
                            "board": board_name,
                            "post_title": post_title,
                            "file_url": url,
                        })

        return _ok({"files": all_files, "count": len(all_files)})
    except Exception as e:
        return _err(str(e))


def download_course_files(
    course_filter: Optional[str] = None,
    dest_dir: Optional[str] = None,
) -> dict:
    """
    수강 과목의 새 파일을 다운로드합니다.
    이미 받은 파일은 state/file_state.json으로 추적해 중복 방지.

    Args:
        course_filter: 특정 과목명 일부 (None이면 전체)
        dest_dir:      저장 경로 (None이면 KLMS_DOWNLOAD_DIR 또는 klms_skills/downloads/)

    Returns:
        {"success": True, "downloaded": [...], "downloaded_count": int, "failed_count": int}
    """
    try:
        files_result = list_course_files(course_filter)
        if not files_result["success"]:
            return files_result

        save_dir = Path(dest_dir) if dest_dir else _get_download_dir()
        session = _make_session()
        state = _load_state("file")
        already: set[str] = set(state.get("downloaded_urls", []))
        fingerprints: dict = dict(state.get("fingerprints", {}))

        downloaded, failed = [], []

        for f in files_result["files"]:
            # pluginfile URL 확인 (resource는 view.php → redirect)
            file_url = f.get("file_url", "")
            if not file_url:
                time.sleep(0.5)
                r = session.get(f["view_url"], allow_redirects=False, timeout=HTTP_TIMEOUT)
                file_url = r.headers.get("Location", "")
                if not file_url:
                    soup = BeautifulSoup(r.text, "html.parser")
                    pa = soup.select_one("a[href*='pluginfile']")
                    file_url = pa.get("href", "") if pa else ""
            if not file_url:
                continue

            filename = unquote(file_url.split("/")[-1].split("?")[0])

            if file_url in already or _file_exists_locally(f["course"], filename, save_dir):
                already.add(file_url)
                continue

            course_dir = _course_dest_dir(f["course"], save_dir)
            dest = course_dir / _dest_subdir(filename) / filename
            dest.parent.mkdir(parents=True, exist_ok=True)

            try:
                time.sleep(FETCH_DELAY)
                dr = session.get(file_url, allow_redirects=True, timeout=60, stream=True)
                if dr.status_code != 200 or "text/html" in dr.headers.get("Content-Type", ""):
                    failed.append({"name": filename, "course": f["course"], "error": f"HTTP {dr.status_code}"})
                    continue
                size = 0
                with open(dest, "wb") as fh:
                    for chunk in dr.iter_content(chunk_size=65536):
                        fh.write(chunk)
                        size += len(chunk)
                already.add(file_url)
                downloaded.append({
                    "course": f["course"],
                    "filename": filename,
                    "size_bytes": size,
                    "saved_to": str(dest),
                })
            except Exception as ex:
                failed.append({"name": filename, "course": f["course"], "error": str(ex)})

        state["downloaded_urls"] = list(already)
        state["fingerprints"] = fingerprints
        state["last_run"] = datetime.now(tz=timezone.utc).isoformat()
        _save_state("file", state)

        return _ok({
            "downloaded": downloaded,
            "failed": failed,
            "downloaded_count": len(downloaded),
            "failed_count": len(failed),
            "message": f"{len(downloaded)}개 다운로드 완료" + (f", {len(failed)}개 실패" if failed else ""),
        })
    except Exception as e:
        return _err(str(e))


# ── 공개 API: 과제 제출 ────────────────────────────────────────────────────

_MIME_MAP = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
    ".txt": "text/plain",
    ".py": "text/x-python",
    ".java": "text/x-java-source",
    ".c": "text/x-c",
    ".cpp": "text/x-c++",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _guess_mime(filename: str) -> str:
    return _MIME_MAP.get(Path(filename).suffix.lower(), "application/octet-stream")


def _extract_fp_data(html: str) -> dict:
    """편집 제출 페이지 HTML에서 draft itemid / client_id / upload repo_id 추출."""
    result = {}

    # hidden input name="*filemanager*" value=숫자 → itemid + 필드명
    soup = BeautifulSoup(html, "html.parser")
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        value = inp.get("value", "")
        if "filemanager" in name and value.isdigit() and len(value) >= 6:
            result["itemid"] = value
            result["fm_field"] = name  # 나중에 저장 폼에 그대로 사용
            break

    # JS init 데이터에서 client_id
    m = re.search(r'"client_id"\s*:\s*"([a-zA-Z0-9_]+)"', html)
    if m:
        result["client_id"] = m.group(1)

    # upload repo id: "type":"upload" 근처의 "id"
    for m in re.finditer(r'"type"\s*:\s*"upload"', html):
        ctx = html[max(0, m.start() - 200): m.end() + 200]
        id_m = re.search(r'"id"\s*:\s*"?(\d+)"?', ctx)
        if id_m:
            result["upload_repo_id"] = id_m.group(1)
            break

    return result


_KR_DATE = re.compile(
    r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일"
    r"(?:\([^)]*\))?\s*(오전|오후)?\s*(\d{1,2}):(\d{2})"
)


_KLMS_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}
# 영어 KLMS: 'Sunday, 8 November 2026, 11:59 PM'
_KLMS_EN_DATETIME = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})\s*([AP]M)", re.I)


def _parse_en_datetime(text: str) -> Optional[str]:
    text = text or ""
    # 퀴즈 안내는 'opened ... / will close ...' 를 한 블록에 담는다. 마감 쪽을 먼저 본다.
    cut = re.search(r"\b(close[sd]?|due)\b", text, re.I)
    m = (cut and _KLMS_EN_DATETIME.search(text, cut.end())) or _KLMS_EN_DATETIME.search(text)
    if not m or m.group(2).lower() not in _KLMS_MONTHS:
        return None
    day, month, year, hour, minute, ampm = m.groups()
    hour = int(hour) % 12 + (12 if ampm.upper() == "PM" else 0)
    try:
        return datetime(int(year), _KLMS_MONTHS[month.lower()], int(day), hour, int(minute)).isoformat()
    except ValueError:
        return None


def _parse_kr_datetime(text: str) -> Optional[str]:
    """한국어와 영어 KLMS 날짜를 모두 읽는다."""
    m = _KR_DATE.search(text or "")
    if not m:
        return _parse_en_datetime(text)
    year, month, day, meridiem, hour, minute = m.groups()
    hour = int(hour)
    if meridiem == "오후" and hour != 12:
        hour += 12
    elif meridiem == "오전" and hour == 12:
        hour = 0
    try:
        return datetime(int(year), int(month), int(day), hour, int(minute)).isoformat()
    except ValueError:
        return None


def _period_end_iso(period: str) -> Optional[str]:
    """과목 페이지의 '2026-10-13 16:00 ~ 2026-10-20 23:59' 에서 끝 시각을 꺼낸다."""
    found = re.findall(r"(\d{4}-\d{2}-\d{2}) (\d{1,2}:\d{2})", period or "")
    if not found:
        return None
    day, hm = found[-1]
    try:
        return datetime.fromisoformat(f"{day}T{int(hm.split(':')[0]):02d}:{hm.split(':')[1]}:00").isoformat()
    except ValueError:
        return None


def _restricted_deadline(cname: str, a: dict) -> dict:
    """오픈 전 활동은 view 페이지가 막혀 있으니 과목 페이지에 적힌 기간으로 마감을 만든다."""
    return {
        "course": cname,
        "type": a["type"],
        "name": a["name"],
        "due_date": a.get("period", ""),
        "due_date_iso": _period_end_iso(a.get("period", "")),
        "submission_status": f"오픈 전 ({a.get('availability', '')})".strip(),
        "url": a["url"],
        "restricted": True,
    }


def get_quiz_status(quiz_url: str) -> dict:
    """
    퀴즈 현황(마감, 응시 상태, 성적 등)을 확인합니다.

    Args:
        quiz_url: 퀴즈 URL (예: https://klms.kaist.ac.kr/mod/quiz/view.php?id=...)

    Returns:
        {"success": True, "name": str, "due_date": str, "attempts": int, ...}
    """
    try:
        session = _make_session()
        r = session.get(quiz_url, timeout=HTTP_TIMEOUT)
        if not _is_session_valid(r.url, r.text):
            return _err("세션 만료 — login()을 먼저 실행하세요")

        soup = BeautifulSoup(r.text, "html.parser")
        main = soup.select_one("div[role=main]") or soup

        name_el = main.select_one("h2, h3")
        name = name_el.get_text(strip=True) if name_el else ""

        def _row(label_kw: str) -> str:
            for row in main.select("tr"):
                th = row.select_one("th, td.c0")
                td = row.select_one("td.c1, td:last-child")
                if th and td and label_kw in th.get_text():
                    return td.get_text(" ", strip=True)
            return ""

        def _info(label_kw: str) -> str:
            for p in main.select("p, div.quizinfo, div.box"):
                text = p.get_text(" ", strip=True)
                if label_kw in text:
                    return text
            return ""

        due_raw = _row("마감") or _row("Close") or _info("마감") or _info("closes")
        due_iso = _parse_kr_datetime(due_raw)
        open_raw = _row("시작") or _row("Open") or _info("시작") or _info("opens")

        attempts_text = _row("응시") or _row("Attempts") or ""
        attempt_count = 0
        am = re.search(r"(\d+)", attempts_text)
        if am:
            attempt_count = int(am.group(1))

        grade_text = _row("성적") or _row("Grade") or ""

        return _ok({
            "name": name,
            "due_date": due_raw,
            "due_date_iso": due_iso,
            "open_date": open_raw,
            "attempts": attempt_count,
            "attempts_text": attempts_text,
            "grade": grade_text,
            "url": quiz_url,
        })
    except Exception as e:
        return _err(str(e))


# ── 공지에 적힌 일정 ─────────────────────────────────────────────────────
# Moodle 마감 조회는 assign/quiz 활동만 본다. MAS355 는 과제를 자료로 올리고
# 퀴즈는 수업 중 지필이라 활동이 없다 — 6일 뒤 퀴즈가 있어도 마감 0건이다.
# 공지 본문은 이미 로컬에 받아두므로 네트워크 없이 읽어서 채운다.

_SCHEDULE_KEYWORDS = (
    "quiz", "퀴즈", "exam", "시험", "중간", "기말", "midterm", "final",
    "due", "마감", "제출", "submit", "hw", "homework", "과제", "숙제",
)
_KO_DATE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_EN_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec")
# 'September 30th' 처럼 서수가 붙는다. \b 로 끝내면 통째로 놓친다 (실측 CS371).
_EN_DATE = re.compile(
    r"\b(" + "|".join(_EN_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
# '9/16'. 앞뒤로 숫자나 슬래시가 붙으면 날짜가 아니다 ('20/100 점', '2026/09/16').
_SLASH_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
# 뒤에 am/pm 이 붙는다. \b 로 끝내면 '23:59pm' 을 통째로 놓친다.
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?(?!\d)", re.I)
_NOTICE_DATE = re.compile(r"notice_(\d{4})-(\d{2})-(\d{2})_")


def _notice_posted_on(path: Path) -> date:
    m = _NOTICE_DATE.search(path.name)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def _dates_in_line(line: str, posted: date) -> list[tuple[int, int, date]]:
    """한 줄에서 날짜를 전부 찾아 (시작, 끝, 날짜) 로 돌려준다.

    공지에는 연도가 없다. 공지가 올라온 해로 읽되, 그보다 한참 과거로
    나오면 다음 해를 가리키는 것이다 (12월 공지의 '1월 5일').
    """
    out = []
    for m in _KO_DATE.finditer(line):
        out.append((m.start(), m.end(), int(m.group(1)), int(m.group(2))))
    for m in _EN_DATE.finditer(line):
        out.append((m.start(), m.end(), _EN_MONTHS.index(m.group(1).lower()) + 1,
                    int(m.group(2))))
    for m in _SLASH_DATE.finditer(line):
        out.append((m.start(), m.end(), int(m.group(1)), int(m.group(2))))
    found = []
    for start, end, month, day in sorted(out):
        try:
            d = date(posted.year, month, day)
        except ValueError:
            continue  # 2월 30일 같은 오타
        if d < posted - timedelta(days=30):
            try:
                d = date(posted.year + 1, month, day)
            except ValueError:
                continue
        found.append((start, end, d))
    return found


# 라벨은 '무엇의 날짜인가' 다. 앞 문장을 통째로 끌고 오면 못 읽는다 (실측:
# '1.9, Some Special Expectations Quiz Date'). 일정 키워드가 시작점이다.
_KEYWORD_ANCHOR = re.compile(
    "|".join(k if not k.isascii() else rf"\b{k}\b" for k in _SCHEDULE_KEYWORDS),
    re.I)


def _label_before(line: str, start: int, prev_end: int = 0) -> str:
    """날짜 앞의 글자에서 항목 이름을 뽑는다 ('- HW 1 공개: ' -> 'HW 1 공개').

    앞 항목의 날짜 뒤에서부터 본다. 한 줄에 여러 항목이 붙어 있을 때
    앞 항목 이름이 뒤 항목에 섞이지 않게 한다.
    """
    head = line[prev_end:start].split("/")[-1]
    anchors = list(_KEYWORD_ANCHOR.finditer(head))
    if anchors:
        head = head[anchors[-1].start():]
    elif ":" in head:
        head = head.rsplit(":", 1)[0]
    head = head.strip(" \t\u2013\u2014-•*[(]:,").strip()
    # 'Quiz: In class on' — 날짜를 받던 전치사가 이름 끝에 남는다.
    head = _DANGLING_TAIL.sub("", head).strip(" \t:,")
    return head[-40:] or "일정"


_DANGLING_TAIL = re.compile(r"(?:\s+(?:on|by|at|until|till|before|is|are|will be|in))+$", re.I)

# 날짜 앞 라벨에 이 말이 있으면 같은 과제의 지각 마감이다. 본 마감과 따로 한 줄이
# 되면 별개 할 일로 뜨고 KLMS 밖 제출로까지 잘못 센다 (실측 2026-09-26 CS371).
# 'calculate'·'later' 에 걸리지 않게 단어로 본다.
_LATE_LABEL = re.compile(r"\blate\b|지각|\bpenalty\b|감점", re.I)

# 휴강 날짜는 할 일이 아니다. 키워드 없는 날짜에만 본다 — 키워드가 붙은 날짜는
# 휴강 공지 안에 있어도 따로 잡힌 일정이다 ('No class Monday; quiz on Oct 5').
_NO_CLASS = re.compile(
    r"no (?:in-person )?(?:lecture|class)|휴강|cancel", re.I)

# 'HW 3 out: Oct. 5' · 'HW 3 공개: 10월 5일' 은 과제가 나오는 날이지 할 일이 아니다.
# 'hw' 키워드에 걸려 마감처럼 떴다 (실측 2026-10-01 MAS355). 날짜 바로 앞 라벨만 본다 —
# 같은 줄 'Quiz 3: Oct. 12' 는 살아야 한다.
_RELEASE_LABEL = re.compile(
    r"\b(?:out|released?|posted|uploaded)\b|공개|게시|출제", re.I)


def _clause_before(line: str, start: int, prev_end: int = 0) -> str:
    """날짜 바로 앞, 앞 날짜나 문장 끝 이후의 글자. 앞 문장의 말이 섞이지 않는다."""
    head = line[prev_end:start].split("/")[-1]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    return head[ends[-1]:] if ends else head


def _has_label_anchor(line: str, start: int, prev_end: int = 0) -> bool:
    """날짜 앞에 일정 키워드가 있는가. 없으면 _label_before() 는 아무 조각이나 준다."""
    return bool(_KEYWORD_ANCHOR.search(line[prev_end:start].split("/")[-1]))


# 'Oct. 5' 의 마침표는 문장 끝이 아니다 (실측 MAS355 일정표: 'Quiz 3: Oct.' 에서 끊겼다).
_SENTENCE_END = re.compile(
    r"(?<!\b(?:" + "|".join(_EN_MONTHS) + r"))(?<!\bsept)[.!?](?=\s|$)", re.I)


def _sentence_around(line: str, start: int, end: int) -> str:
    """날짜가 든 문장 하나. 공지 본문은 한 줄이라 줄 전체는 공지 전체다."""
    before = [m.end() for m in _SENTENCE_END.finditer(line, 0, start)]
    left = before[-1] if before else 0
    after = _SENTENCE_END.search(line, end)
    right = after.end() if after else len(line)
    return line[left:right].strip()


def _read_notice(path: Path) -> Optional[tuple[str, str, str, list[str]]]:
    """저장된 공지를 (제목, 원문 URL, 본문, 줄들) 로 읽는다. 본문은 '---' 아래 전부."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), "")
    url = next((l.split(":", 1)[1].strip() for l in lines
                if l.startswith("- 원문:")), "")
    body = "\n".join(lines[lines.index("---") + 1:]).strip() if "---" in lines else ""
    return title, url, body, lines


def _schedule_today() -> date:
    return date.today()


# 제출을 요구하는 말. 퀴즈·시험 공지와 갈라내려고 _SCHEDULE_KEYWORDS 와 따로 둔다 —
# 저쪽은 '일정이 적힌 줄'을 찾는 그물이고, 이쪽은 '내가 낼 물건이 있는가'를 묻는다.
# 굴절형을 놓치면 마감이 숨으므로 부분 문자열로 본다 ('submission', 'resubmit', '제출한').
# 제출 상태 칸. 한국어 '시도하지 않음', 영어 'No attempt' / 'No submission'.
_NOT_SUBMITTED = ("하지 않음", "No attempt", "No submission", "Not submitted")
_SUBMIT_WORDS = ("submit", "submission", "제출", "due", "마감", "deadline", "hand in")


def _asks_for_a_submission(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in _SUBMIT_WORDS)


def _already_submitted(course: str, due_iso: str) -> bool:
    """KLMS 밖(Gradescope 등)에 낸 과제는 KLMS 가 모른다. 공개판은 따로 기록하지 않는다."""
    return False


# KLMS 는 마감을 '9월 16일 오전 12:00'(자정)으로, 공지 본문은 '9/15 23:59' 로 적는다.
# 1분 차이로 날짜가 갈리므로 날짜만 비교하면 같은 마감이 두 번 들어온다.
_SAME_DEADLINE_WINDOW = timedelta(hours=2)


# 앉아야 하는 일. 낼 물건이 있는 일과 갈라야 같은 날 둘 다 있어도 안 접힌다.
_EXAM_WORDS = ("quiz", "퀴즈", "exam", "시험", "midterm", "final", "중간", "기말")


def _is_exam(text: str) -> bool:
    return any(w in (text or "").lower() for w in _EXAM_WORDS)


def _same_deadline(hit: dict, covered: tuple[str, str, str]) -> bool:
    """공지에서 읽은 일정이 이미 잡힌 KLMS 마감과 같은 것인가."""
    course, iso, ctype = covered
    if hit["course"] != course or not iso:
        return False
    # 퀴즈와 과제는 같은 날 나란히 있을 수 있다. 종류가 다르면 다른 일이다.
    # 종류는 name 으로만 가린다. due_date 는 날짜가 든 문장이라 옆 항목의 'Quiz' 가
    # 섞인다 (실측: 'Due Date: Sep 29 ... Quiz: In class on Sep 30' 에서 과제 마감이
    # 퀴즈로 읽혀 KLMS 과제와 두 줄로 떴다).
    if _is_exam(hit["name"]):
        if ctype != "quiz":
            return False
    elif ctype == "quiz":
        return False
    if hit["due_date_iso"][:10] == iso[:10]:
        return True
    try:  # 날짜가 갈렸어도 두 시간 안이면 같은 마감이다
        a = datetime.fromisoformat(hit["due_date_iso"])
        b = datetime.fromisoformat(iso)
    except ValueError:
        return False  # 한쪽이 날짜뿐이면 위 날짜 비교가 유일한 근거다
    return abs(a - b) <= _SAME_DEADLINE_WINDOW


def _schedule_from_notices(base, days_ahead: int, today: Optional[date] = None,
                           now: Optional[datetime] = None,
                           course_names: Optional[list[str]] = None) -> list[dict]:
    """로컬 공지에서 이 창 안에 드는 일정을 읽어낸다."""
    base = Path(base)
    if today is None:
        today = now.date() if now else _schedule_today()
        if now is None and today == date.today():
            now = datetime.now()
    until = today + timedelta(days=days_ahead)
    # 과목 폴더는 _course_dest_dir 이 과목명으로 만든다. 공지가 저장된 폴더를 전부 본다.
    # 폴더명은 '/' 같은 글자를 '_' 로 바꾼 것이라, 원래 과목명으로 되돌려야 마감 중복 제거가 맞는다.
    mapped = {_course_dest_dir(n, base).name: n for n in (course_names or [])}
    mapped.update({v: k for k, v in _COURSE_FOLDER_MAP.items()})
    folder_to_course = {
        d.name: mapped.get(d.name, d.name)
        for d in (sorted(base.iterdir()) if base.is_dir() else [])
        if (d / "notices").is_dir() and (not course_names or d.name in mapped)
    }

    hits: list[dict] = []
    past: set[tuple] = set()
    for folder, course in folder_to_course.items():
        for path in sorted((base / folder / "notices").glob("*.md")):
            posted = _notice_posted_on(path)
            notice = _read_notice(path)
            if notice is None:
                continue
            title, url, body, lines = notice
            for line in lines:
                if not any(k in line.lower() for k in _SCHEDULE_KEYWORDS):
                    continue
                spots = _dates_in_line(line, posted)
                for i, (start, end, d) in enumerate(spots):
                    if not (today <= d <= until):
                        continue
                    # 시각은 다음 날짜가 나오기 전까지만 본다. 안 그러면 뒤쪽
                    # 항목의 시각을 앞 항목에 붙인다.
                    stop = spots[i + 1][0] if i + 1 < len(spots) else len(line)
                    tm = _TIME.search(line, end, stop)
                    iso = d.isoformat()
                    if tm and int(tm.group(1)) < 24:
                        hour = int(tm.group(1))
                        meridiem = (tm.group(3) or "").lower().replace(".", "")
                        if meridiem == "pm" and hour < 12:
                            hour += 12
                        elif meridiem == "am" and hour == 12:
                            hour = 0
                        iso += f"T{hour:02d}:{tm.group(2)}:00"
                        # 오늘 일정이라도 시각이 지났으면 이미 끝난 일이다 (실측 10/12
                        # 19시: 14:30 에 본 Quiz 3 이 '오늘' 로 떴다).
                        if now and datetime.fromisoformat(iso) < now:
                            past.add((course, iso[:10]))
                            continue
                    prev_end = spots[i - 1][1] if i else 0
                    # 날짜 앞에 키워드가 없으면 조각이 이름이 된다 (실측: ') and next
                    # Monday'). 그럴 땐 공지 제목이 낫다.
                    anchored = _has_label_anchor(line, start, prev_end)
                    sentence = _sentence_around(line, start, end)
                    if not anchored and _NO_CLASS.search(sentence):
                        continue
                    if anchored and _LATE_LABEL.search(_clause_before(line, start, prev_end)):
                        continue
                    if anchored and _RELEASE_LABEL.search(_label_before(line, start, prev_end)):
                        continue
                    name = (_label_before(line, start, prev_end) if anchored or not title
                            else title)
                    hits.append({
                        "course": course,
                        "type": "notice",
                        "name": name,
                        "due_date": sentence,
                        "due_date_iso": iso,
                        "source_notice": str(path),
                        # 한 문장만으로는 맥락이 빠진다. 공지를 통째로 싣는다.
                        "notice_title": title,
                        "notice_url": url,
                        "notice_body": body,
                        "_anchored": anchored,
                    })

    # 같은 일정이 여러 공지에 나온다 (학기 일정표 + 해당 주 공지). 하루에 하나만
    # 남기되 시각까지 적힌 쪽을 고른다.
    best: dict[tuple, dict] = {}
    for h in hits:
        key = (h["course"], h["due_date_iso"][:10])
        # 학기 일정표는 날짜만 적는다. 같은 날 시각이 적힌 공지가 이미 지났으면
        # 일정표 줄도 같은 일이다.
        if key in past and "T" not in h["due_date_iso"]:
            continue
        cur = best.get(key)
        rank = ("T" in h["due_date_iso"], h["_anchored"], len(h["name"]))
        if cur is None or rank > ("T" in cur["due_date_iso"], cur["_anchored"],
                                  len(cur["name"])):
            best[key] = h
    for h in best.values():
        del h["_anchored"]
    return sorted(best.values(), key=lambda h: h["due_date_iso"])


# ── 타임라인(action events) ───────────────────────────────────────────────

_EVENT_SUFFIXES = (" is due", " 마감", " 이(가) 마감됨", " 개시", " closes")


def _clean_event_name(name: str) -> str:
    """'HW 1 (Due: 9/25) is due' → 'HW 1 (Due: 9/25)'."""
    out = (name or "").strip()
    for suf in _EVENT_SUFFIXES:
        if out.endswith(suf):
            out = out[: -len(suf)].strip()
            break
    return out


def _calendar_action_events(days_ahead: int = 14, back_days: int = 7) -> tuple[list[dict], list[str]]:
    """Moodle 타임라인으로 마감을 훑는다 — 과목 페이지를 믿을 수 없기 때문이다.

    KLMS 과목 첫 화면은 '현재 주차' 근처 섹션만 렌더한다. 지난 주차에 걸린
    과제는 아직 열려 있고 미제출이어도 활동 목록에서 통째로 사라진다
    (실측 2026-09-21: 응용해석학 HW1, 9/25 마감 미제출인데 과정공통 +
    3~5주차만 나오는 페이지에서 cmid 자체가 HTML에 없었다. 하루 전 sweep 에는
    잡혔다). 타임라인 API 는 섹션 렌더와 무관하게 '해야 할 일'을 준다.

    Returns:
        ([{course, course_id, type, name, url, due_date_iso}], notes)
    """
    notes: list[str] = []
    try:
        session = _make_session()
        sesskey = _get_sesskey(session)
        if not sesskey:
            return [], ["타임라인 조회 실패: sesskey 추출 실패"]

        now = int(time.time())
        args = {
            "limitnum": 50,
            "timesortfrom": now - back_days * 86400,
            "limittononsuspendedevents": True,
        }
        if days_ahead:
            args["timesortto"] = now + days_ahead * 86400
        r = session.post(
            f"{KLMS_BASE}/lib/ajax/service.php"
            f"?sesskey={sesskey}&info=core_calendar_get_action_events_by_timesort",
            json=[{"index": 0,
                   "methodname": "core_calendar_get_action_events_by_timesort",
                   "args": args}],
            timeout=HTTP_TIMEOUT,
        )
        body = r.json()[0]
        if body.get("error"):
            return [], [f"타임라인 조회 실패: {body.get('exception', {}).get('message', body['error'])}"]

        out = []
        for e in body.get("data", {}).get("events", []):
            url = _norm_url(e.get("url") or "")
            if not url:
                continue
            course = e.get("course") or {}
            ts = e.get("timesort")
            out.append({
                "course": course.get("fullname") or course.get("shortname") or "",
                "course_id": str(course.get("id") or ""),
                "type": e.get("modulename") or "unknown",
                "name": _clean_event_name(e.get("name", "")),
                "url": url,
                "due_date_iso": (datetime.fromtimestamp(ts).isoformat() if ts else None),
            })
        return out, notes
    except Exception as e:
        return [], [f"타임라인 조회 실패: {e}"]


def get_upcoming_deadlines(days_ahead: int = 14) -> dict:
    """
    전체 수강 과목의 다가오는 마감(과제 + 퀴즈)을 한번에 조회합니다.

    Args:
        days_ahead: 앞으로 며칠 이내의 마감만 반환 (기본 14일)

    Returns:
        {"success": True, "deadlines": [...], "notes": [...]}
    """
    try:
        courses_result = get_courses()
        if not courses_result["success"]:
            return courses_result

        session = _make_session()
        deadlines = []
        notes = []

        for course in courses_result["courses"]:
            cname = course["name"]
            acts = get_course_activities(cname)
            if not acts.get("success"):
                notes.append(f"{cname}: 활동 조회 실패 ({acts.get('error')})")
                continue

            activities = acts.get("activities", [])
            assigns = [a for a in activities if a.get("type") == "assign"]
            quizzes = [a for a in activities if a.get("type") == "quiz"]
            ltis = [a for a in activities
                    if a.get("type") == "lti" and _is_graded_lti(a)]

            if not assigns and not quizzes:
                if ltis:
                    notes.append(f"{cname}: KLMS 과제/퀴즈 없음, 외부 도구 {len(ltis)}개")
                continue

            for a in assigns:
                if a.get("restricted"):
                    deadlines.append(_restricted_deadline(cname, a))
                    continue
                time.sleep(FETCH_DELAY)
                status = get_assignment_status(a["url"])
                if not status.get("success"):
                    # 조용히 빠지면 '과제가 없다'와 구별되지 않는다.
                    notes.append(f"{cname}: '{a['name']}' 상태 조회 실패 ({status.get('error')})")
                    continue
                due_raw = status.get("due_date", "")
                due_iso = _parse_kr_datetime(due_raw)
                deadlines.append({
                    "course": cname,
                    "type": "assign",
                    "name": a["name"],
                    "due_date": due_raw,
                    "due_date_iso": due_iso,
                    "submission_status": status.get("submission_status", ""),
                    "url": a["url"],
                })

            for q in quizzes:
                if q.get("restricted"):
                    deadlines.append(_restricted_deadline(cname, q))
                    continue
                time.sleep(FETCH_DELAY)
                status = get_quiz_status(q["url"])
                if not status.get("success"):
                    notes.append(f"{cname}: '{q.get('name', '')}' 상태 조회 실패 ({status.get('error')})")
                    continue
                deadlines.append({
                    "course": cname,
                    "type": "quiz",
                    "name": q.get("name", "") or status.get("name", ""),
                    "due_date": status.get("due_date", ""),
                    "due_date_iso": status.get("due_date_iso"),
                    "attempts": status.get("attempts", 0),
                    "url": q["url"],
                })

        # 과목 페이지만 믿으면 안 된다 — 현재 주차 근처만 렌더하므로 지난 주차에
        # 걸린 과제는 아직 열려 있어도 활동 목록에서 사라진다. 타임라인으로 한 번 더
        # 훑어 빠진 것을 주워 담는다. [[_calendar_action_events]] 의 주석 참고.
        seen_urls = {d.get("url") for d in deadlines}
        cal_events, cal_notes = _calendar_action_events(days_ahead)
        notes.extend(cal_notes)
        for ev in cal_events:
            if ev["url"] in seen_urls or ev["type"] not in ("assign", "quiz"):
                continue
            time.sleep(FETCH_DELAY)
            if ev["type"] == "assign":
                status = get_assignment_status(ev["url"])
                if not status.get("success"):
                    notes.append(f"{ev['course']}: '{ev['name']}' 상태 조회 실패 ({status.get('error')})")
                    continue
                due_raw = status.get("due_date", "")
                deadlines.append({
                    "course": ev["course"],
                    "type": "assign",
                    "name": ev["name"],
                    "due_date": due_raw,
                    "due_date_iso": _parse_kr_datetime(due_raw) or ev["due_date_iso"],
                    "submission_status": status.get("submission_status", ""),
                    "url": ev["url"],
                    "from_timeline": True,
                })
            else:
                status = get_quiz_status(ev["url"])
                if not status.get("success"):
                    notes.append(f"{ev['course']}: '{ev['name']}' 상태 조회 실패 ({status.get('error')})")
                    continue
                deadlines.append({
                    "course": ev["course"],
                    "type": "quiz",
                    "name": ev["name"] or status.get("name", ""),
                    "due_date": status.get("due_date", ""),
                    "due_date_iso": status.get("due_date_iso") or ev["due_date_iso"],
                    "attempts": status.get("attempts", 0),
                    "url": ev["url"],
                    "from_timeline": True,
                })
            notes.append(
                f"{ev['course']}: '{ev['name']}' 은 과목 페이지에 없고 타임라인에만 있다"
                " — 지난 주차 섹션이 렌더되지 않은 것으로 보인다"
            )

        if days_ahead:
            now = datetime.now()
            cutoff = now.replace(hour=0, minute=0, second=0) + timedelta(days=days_ahead)
            filtered = []
            for d in deadlines:
                iso = d.get("due_date_iso")
                if not iso:
                    filtered.append(d)
                    continue
                try:
                    dt = datetime.fromisoformat(iso)
                    if dt <= cutoff:
                        filtered.append(d)
                except ValueError:
                    filtered.append(d)
            deadlines = filtered

        deadlines.sort(key=lambda d: d.get("due_date_iso") or "9999")

        return _ok({
            "deadlines": deadlines,
            "count": len(deadlines),
            "notes": notes,
            "message": f"마감 {len(deadlines)}건" + (f", 참고 {len(notes)}건" if notes else ""),
        })
    except Exception as e:
        return _err(str(e))


def get_assignment_status(assign_url: str) -> dict:
    """
    과제 현황(제출 상태, 기한, 허용 파일 형식 등)을 확인합니다.

    Args:
        assign_url: 과제 URL (예: https://klms.kaist.ac.kr/mod/assign/view.php?id=1231535)

    Returns:
        {"success": True, "status": str, "due_date": str, "submission_status": str, ...}
    """
    try:
        session = _make_session()
        r = session.get(assign_url, timeout=HTTP_TIMEOUT)
        if not _is_session_valid(r.url, r.text):
            return _err("세션 만료 — login()을 먼저 실행하세요")

        soup = BeautifulSoup(r.text, "html.parser")
        main = soup.select_one("div[role=main]") or soup

        def _row(label_kw: str) -> str:
            for row in main.select("tr"):
                th = row.select_one("th, td.c0")
                td = row.select_one("td.c1, td:last-child")
                if th and td and label_kw in th.get_text():
                    return td.get_text(" ", strip=True)
            return ""

        can_edit = bool(
            main.select_one("button#id_submitbutton") or
            soup.find("button", string=re.compile("제출 추가|편집|Edit submission"))
        )

        # 과제 본문. 상태 표만 긁으면 '무엇을 하라는 과제인지'가 통째로 빠진다.
        # MAS202 처럼 첨부 없이 외부 교재 링크 + 문제 번호만 본문에 적는 과목이 있다.
        intro_el = (
            main.select_one("#intro")
            or main.select_one("div.activity-description")
            or main.select_one("div.box.generalbox:not(.boxaligncenter)")
        )
        intro = ""
        attachments: list[str] = []
        external_links: list[str] = []
        if intro_el:
            intro = intro_el.get_text("\n", strip=True)
            for a in intro_el.find_all("a", href=True):
                href = _norm_url(a["href"])
                if "pluginfile.php" in href:
                    if href not in attachments:
                        attachments.append(href)
                elif href.startswith("http") and KLMS_BASE not in href:
                    if href not in external_links:
                        external_links.append(href)
            # 링크 없이 URL 을 본문에 그대로 적어두는 경우가 흔하다.
            for m in re.finditer(r"https?://[^\s<>\"']+", intro):
                href = m.group(0).rstrip(".,)")
                if KLMS_BASE in href or href in external_links or href in attachments:
                    continue
                external_links.append(href)

        # 라벨은 '마감 일시'(띄어쓰기), '마지막 수정' 처럼 표기가 흔들린다.
        # 접두 키워드로 느슨하게 잡는다 — 정확한 문구를 고정하면 조용히 빈 값이 된다.
        return _ok({
            "submission_status": _row("제출 상태") or _row("Submission status"),
            "grading_status": _row("채점 상태") or _row("Grading status"),
            "due_date": _row("마감") or _row("Due date"),
            "time_remaining": _row("남은 시간") or _row("Time remaining"),
            "last_modified": _row("마지막") or _row("Last modified"),
            "grade": _row("성적") or _row("Grade"),
            "can_edit": can_edit,
            "intro": intro,
            "attachments": attachments,
            "external_links": external_links,
            "url": assign_url,
        })
    except Exception as e:
        return _err(str(e))


# ── 과제 외부자료 / 로컬 산출물 ──────────────────────────────────────────
# 과제 본문이 KLMS 밖 교재 PDF 를 가리키는 과목이 있다(MAS202: 첨부 없이
# 외부 링크 + 문제 번호만). 링크를 문자열로만 돌려주면 "이미 받아뒀는지" 를
# 호출자가 알 수 없어 매번 사람 기억에 의존하게 된다 — 실제로 교재를 받아
# 과목 폴더에 넣어두고도 다음 점검에서는 그 사실이 사라졌다. 그래서 받은 것을
# state 에 남기고, 호출 시점에 디스크까지 확인해서 알려준다.

_EXTERNAL_DOC_EXTS = (".pdf", ".ps", ".dvi", ".djvu", ".zip", ".tex", ".ipynb")
_EXTERNAL_MAX_BYTES = 64 * 1024 * 1024


def _assignment_key(name: str) -> Optional[str]:
    """'HW 1 (Due: 9/25) 과제' → 'hw1'. 괄호 안 마감일의 숫자에 낚이면 안 된다."""
    base = re.sub(r"\([^)]*\)", " ", name or "")
    m = re.search(r"([A-Za-z]{2,12})\s*0*(\d{1,3})\b", base)
    return f"{m.group(1).lower()}{int(m.group(2))}" if m else None


def _normalize_stem(stem: str) -> str:
    """hw01_solution → hw1solution. 0 패딩 유무로 매칭이 갈리지 않게."""
    s = re.sub(r"[^a-z0-9]", "", stem.lower())
    return re.sub(r"(?<=[a-z])0+(?=\d)", "", s)


def _save_assign_attachments(course: str, urls: list[str], base) -> tuple[list[str], list[dict]]:
    """과제 첨부를 {과목}/assignments/ 에 받는다. 이미 있으면 건너뛴다.

    Returns: (저장된 경로 전부, 이번에 새로 받은 파일의 downloaded 항목)
    """
    course_dir = _course_dest_dir(course, Path(base))
    out_dir = course_dir / "assignments"
    paths: list[str] = []
    new_files: list[dict] = []
    session = None
    for url in urls:
        # 사람이나 Codex 가 과제 폴더로 옮겨 둔 사본이 있으면 그걸 쓴다. 다시 받으면 같은 파일이 두 벌 생긴다.
        dest = _find_local_copy(course_dir, _asset_name(url)) or out_dir / _asset_name(url)
        if not dest.is_file():
            try:
                session = session or _make_session()
                r = session.get(url, timeout=HTTP_TIMEOUT)
            except Exception:
                continue
            if getattr(r, "status_code", 200) != 200 or not r.content:
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(r.content)
            tmp.replace(dest)
            new_files.append({
                "course": course, "filename": dest.name, "size_bytes": len(r.content),
                "saved_to": str(dest), "reason": "assign_attachment",
            })
        paths.append(str(dest))
    return paths, new_files


def _local_artifacts(course: str, name: str, base) -> list[str]:
    """이 과제에 대해 이미 만들어둔 로컬 파일(풀이 메모, 스캔 등)."""
    key = _assignment_key(name)
    if not key:
        return []
    course_dir = _course_dest_dir(course, Path(base))
    if not course_dir.is_dir():
        return []
    return [
        str(f) for f in _iter_course_files(course_dir)
        if _stem_matches_key(_normalize_stem(f.stem), key)
    ]


def _stem_matches_key(stem: str, key: str) -> bool:
    """hw1 은 hw1_sol 에 붙지만 hw10 에는 붙으면 안 된다."""
    return stem.startswith(key) and not stem[len(key):len(key) + 1].isdigit()


def _is_doc_link(url: str) -> bool:
    return url.split("?")[0].lower().endswith(_EXTERNAL_DOC_EXTS)


def _external_filename(url: str) -> str:
    name = unquote(url.split("?")[0].rstrip("/").split("/")[-1])
    return re.sub(r'[\\/:*?"<>|]', "_", name) or "external"


def _fetch_external_doc(url: str) -> bytes:
    """외부 문서를 받아 바이트로 돌려준다.

    KLMS 세션 쿠키가 붙은 _make_session() 을 쓰지 않는다 — 남의 서버에 학교
    세션 쿠키를 보낼 이유가 없다.
    """
    r = requests.get(url, headers=_HEADERS, allow_redirects=True, timeout=60, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    ctype = r.headers.get("Content-Type", "")
    if "text/html" in ctype:
        raise RuntimeError(f"문서가 아님 ({ctype})")
    buf = bytearray()
    for chunk in r.iter_content(chunk_size=65536):
        buf += chunk
        if len(buf) > _EXTERNAL_MAX_BYTES:
            raise RuntimeError("파일이 너무 큼 (>64MB)")
    return bytes(buf)


def _find_identical_file(course_dir: Path, content: bytes):
    """같은 내용이 다른 이름으로 이미 있으면 그 경로.

    링크 basename 과 사람이 붙인 이름은 다르다(diffeq.pdf ↔
    Taylor_DiffEq_textbook.pdf). 이름만 보면 쌍둥이 파일이 생긴다.
    """
    if not course_dir.is_dir():
        return None
    size = len(content)
    digest = hashlib.sha256(content).hexdigest()
    for f in _iter_course_files(course_dir):
        try:
            if f.stat().st_size != size:
                continue
            if hashlib.sha256(f.read_bytes()).hexdigest() == digest:
                return f
        except OSError:
            continue
    return None


def _resolve_external_links(
    links: list,
    course: str,
    base=None,
    fetch: bool = True,
) -> list:
    """외부 링크마다 로컬에 있는지 확인하고, 없으면(fetch) 받아서 기록한다.

    state 만 믿지 않는다. 사용자가 파일을 지우거나 옮기면 기록은 남아도
    파일은 없다. 매번 실제 경로를 확인한다.

    status: saved(이미 있음) / fetched(방금 받음) / adopted(같은 내용의 기존
            파일을 채택) / missing(없는데 안 받음) / skipped(문서 아님) / error
    """
    base = Path(base) if base else _get_download_dir()
    state = _load_state("external")
    known = dict(state.get("links", {}))
    out, dirty = [], False

    for url in links:
        rec = known.get(url) or {}
        local = rec.get("local_path", "")
        if local and Path(local).is_file():
            out.append({"url": url, "status": "saved",
                        "local_path": local, "filename": Path(local).name})
            continue
        if not _is_doc_link(url):
            out.append({"url": url, "status": "skipped", "reason": "문서 링크 아님"})
            continue
        if not fetch:
            out.append({"url": url, "status": "missing"})
            continue
        try:
            content = _fetch_external_doc(url)
        except Exception as ex:
            out.append({"url": url, "status": "error", "error": str(ex)})
            continue

        course_dir = _course_dest_dir(course, base)
        twin = _find_identical_file(course_dir, content)
        if twin is not None:
            dest, status = twin, "adopted"
        else:
            course_dir.mkdir(parents=True, exist_ok=True)
            dest = course_dir / _external_filename(url)
            dest.write_bytes(content)
            status = "fetched"

        known[url] = {
            "local_path": str(dest),
            "course": course,
            "size": len(content),
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        dirty = True
        out.append({"url": url, "status": status,
                    "local_path": str(dest), "filename": dest.name})

    if dirty:
        state["links"] = known
        state["last_run"] = datetime.now(tz=timezone.utc).isoformat()
        _save_state("external", state)
    return out


# ── 성적부 ──────────────────────────────────────────────────────────────

def _grade_num(text: str) -> Optional[float]:
    try:
        return float(text.replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _parse_grade_report(html: str, keep_weight: bool = False) -> list[dict]:
    """성적부(grade/report/user) 표에서 점수가 매겨진 항목만 뽑는다.

    분류 머리줄(quiz1)은 아래 항목 이름 앞에 붙이고, 합계줄(baggt)은 버린다 —
    합계 범위는 Moodle 이 하위 항목 만점을 그냥 더한 값이라(CS371 quiz1 0–400)
    실제 만점과 다르다.
    """
    table = BeautifulSoup(html, "html.parser").select_one("table.user-grade")
    if not table:
        return []
    items: list[dict] = []
    cats: dict[int, str] = {}
    for tr in table.select("tr"):
        th = tr.select_one("th.column-itemname")
        if not th:
            continue
        cls = th.get("class", [])
        level = next((int(c[5:]) for c in cls
                      if c.startswith("level") and c[5:].isdigit()), 0)
        name = th.get_text(" ", strip=True)
        if "baggt" in cls or "header" in cls:
            continue
        if "item" not in cls:            # 분류 머리줄 — level1 은 과목명 자체
            cats = {k: v for k, v in cats.items() if k < level}
            if level > 1:
                cats[level] = name
            continue

        def cell(col: str) -> str:
            td = tr.select_one(f"td.column-{col}")
            return td.get_text(" ", strip=True) if td else ""

        score = _grade_num(cell("grade"))
        if score is None:                # '-' : 아직 채점 전
            continue
        rng = re.match(r"\s*(-?[\d.,]+)\s*[–-]\s*(-?[\d.,]+)", cell("range"))
        lo, max_score = (_grade_num(rng.group(1)), _grade_num(rng.group(2))) if rng else (0.0, None)
        # 범위 칸은 소수점 없이 반올림된다(0.5 점 만점이 '0–1'). 백분율이 진짜 만점을 준다.
        pct = _grade_num(cell("percentage"))
        if pct and lo is not None:
            max_score = round(lo + (score - lo) * 100 / pct, 2)
        path = [cats[k] for k in sorted(cats) if k < level] + [name]
        items.append({
            "item": " / ".join(path),
            "score": score,
            "max_score": max_score,
            "average": _grade_num(cell("average")),
            "feedback": cell("feedback"),
            "_cat": tuple(path[:-1]),
            "_weight": _grade_num(cell("weight")),
            "_solved": bool(pct),
        })

    # 0 점(또는 최저점)이라 백분율로 못 푼 문항은 같은 분류에서 푼 문항의 만점/가중치 비로 잡는다.
    ratios = {}
    for g in items:
        if g["_solved"] and g["_weight"] and g["max_score"]:
            ratios.setdefault(g["_cat"], g["max_score"] / g["_weight"])
    for g in items:
        ratio = ratios.get(g["_cat"])
        if not g["_solved"] and g["_weight"] and ratio:
            g["max_score"] = round(g["_weight"] * ratio, 2)
        if keep_weight:
            g["weight"] = g["_weight"]
        for k in ("_cat", "_weight", "_solved"):
            del g[k]
    return items


def get_grades() -> dict:
    """
    수강 과목 성적부를 전부 읽는다. 알림에 성적이 안 떠서(2026-10-02 실측) 직접 본다.

    Returns:
        {"success": True, "grades": [{"course": "CS371", "item": ..., "score": ...}],
         "errors": [...]}
    """
    courses = get_courses()
    if not courses.get("success"):
        return courses
    session = _make_session()
    grades: list[dict] = []
    errors: list[str] = []
    for c in courses["courses"]:
        code = _COURSE_FOLDER_MAP.get(c["name"]) or c["name"]
        try:
            r = session.get(f"{KLMS_BASE}/grade/report/user/index.php?id={c['id']}",
                            timeout=HTTP_TIMEOUT)
            for g in _parse_grade_report(r.text):
                grades.append({"course": code, **g})
        except Exception as e:
            errors.append(f"{code}: {e}")
    return _ok({"grades": grades, "errors": errors})


_ATTEND_COLS = ("출석", "결석", "지각", "기타")


def _parse_attendance(html: str) -> list[dict]:
    """출결표(local/lmsattendance)에서 ○ 가 찍힌 칸만 뽑는다. 빈 줄은 '안 부른 날' 이다."""
    table = BeautifulSoup(html, "html.parser").select_one("table.attendance_my")
    if not table:
        return []
    rows = []
    for tr in table.select("tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if len(tds) < 6:
            continue
        week = re.search(r"\d+", tds[0])
        period = re.search(r"\d+", tds[1])
        status = next((name for name, v in zip(_ATTEND_COLS, tds[2:6]) if v), None)
        if week and period and status:
            rows.append({"week": int(week.group()), "period": int(period.group()),
                         "status": status})
    return rows


def get_attendance() -> dict:
    """
    수강 과목 출결표를 전부 읽는다.

    Returns:
        {"success": True, "attendance": {"HSS20159": [{"week", "period", "status"}]},
         "errors": [...]}
    """
    courses = get_courses()
    if not courses.get("success"):
        return courses
    session = _make_session()
    out: dict[str, list[dict]] = {}
    errors: list[str] = []
    for c in courses["courses"]:
        code = _COURSE_FOLDER_MAP.get(c["name"]) or c["name"]
        try:
            r = session.get(f"{KLMS_BASE}/local/lmsattendance/index.php?id={c['id']}",
                            timeout=HTTP_TIMEOUT)
            out[code] = _parse_attendance(r.text)
        except Exception as e:
            errors.append(f"{code}: {e}")
    return _ok({"attendance": out, "errors": errors})


def _record_grades(grades: list[dict]) -> list[dict]:
    """성적을 state/grades.json 에 쌓고, 새로 나왔거나 바뀐 것만 돌려준다."""
    seen = _load_state("grades")
    # 처음 돌 때는 기준만 잡는다. 이미 있던 성적을 전부 '새 성적'으로 알리지 않는다.
    # 성적이 하나도 없어도 기준을 잡았다는 표시는 남겨야 다음 첫 성적을 알린다.
    first = not seen.get("_seeded")
    seen["_seeded"] = True
    fresh = []
    for g in grades:
        key = f"{g['course']}|{g['item']}"
        val = [g.get("score"), g.get("max_score")]
        if seen.get(key) != val:
            if not first and g.get("score") not in (None, "", "-"):
                fresh.append(g)
            seen[key] = val
    _save_state("grades", seen)
    return fresh


def _course_names() -> list[str]:
    try:
        return [c["name"] for c in get_courses().get("courses", [])]
    except Exception:
        return []


# ── 공개 API: 원샷 점검 ──────────────────────────────────────────────────

def sweep(
    days_ahead: int = 21,
    dest_dir: Optional[str] = None,
    include_seen_notices: bool = False,
    fetch_external: bool = True,
) -> dict:
    """
    KLMS 를 한 번에 훑는다 — 새 파일 다운로드 + 공지 + 마감/과제(본문 포함).

    "klms 확인" 한 마디에 해당하는 전부를 이 함수 하나가 한다. 개별 도구를
    호출자가 순서대로 엮게 두면 매번 빠뜨리는 게 생긴다 — 실제로 과제 본문이
    그렇게 몇 주 동안 안 보였다.

    Args:
        days_ahead:           마감 조회 범위 (기본 21일)
        dest_dir:             저장 위치. 그 아래 과목별 폴더에 lectures, assignments, notices 로 나눠 받는다.
        include_seen_notices: True 면 이미 확인한 공지도 함께 반환
        fetch_external:       True 면 과제 본문의 외부 문서(교재 PDF 등)도 받아둔다

    Returns:
        {"success": True, "summary": {...}, "downloaded": [...],
         "notices": [...], "deadlines": [...], "warnings": [...]}
    """
    warnings: list[str] = []
    # '조회가 실패했다'와 'KLMS 에 과제가 없더라' 같은 안내를 섞으면 안 된다.
    # 세션을 다시 짚어보는 건 진짜 실패했을 때뿐이다.
    fetch_failed = False

    sess = check_session()
    if not sess.get("session_valid"):
        return _err("세션 만료 — klms_login() 을 먼저 실행하세요")

    # 1. 새 파일 + 갱신된 파일
    downloaded: list[dict] = []
    updated: list[dict] = []
    dl = download_new_files_fast(dest_dir=dest_dir)
    if dl.get("success"):
        downloaded = dl.get("downloaded", [])
        updated = dl.get("updated", [])
        for e in dl.get("errors", []):
            warnings.append(f"파일 다운로드: {e}")
    else:
        warnings.append(f"파일 다운로드 실패: {dl.get('error')}")
        fetch_failed = True

    # 2. 공지
    notices: list[dict] = []
    nt = get_course_notices(include_seen=include_seen_notices, dest_dir=dest_dir)
    if nt.get("success"):
        notices = nt.get("new_notices", [])
    else:
        warnings.append(f"공지 조회 실패: {nt.get('error')}")
        fetch_failed = True

    # 3. 마감 + 과제 본문 + 외부자료/로컬 산출물
    base_dir = Path(dest_dir) if dest_dir else _get_download_dir()
    deadlines: list[dict] = []
    external_fetched = 0
    dd = get_upcoming_deadlines(days_ahead=days_ahead)
    if dd.get("success"):
        deadlines = dd.get("deadlines", [])
        warnings.extend(dd.get("notes", []))
        for d in deadlines:
            # 퀴즈든 과제든 '내가 이미 뭘 해뒀는지' 는 똑같이 궁금하다.
            d["local_artifacts"] = _local_artifacts(
                d.get("course", ""), d.get("name", ""), base_dir
            )
            if d.get("type") != "assign":
                continue
            # 마감 목록만으로는 '뭘 내라는 건지' 를 알 수 없다. 본문을 붙인다.
            detail = get_assignment_status(d["url"])
            if detail.get("success"):
                d["intro"] = detail.get("intro", "")
                d["attachments"] = detail.get("attachments", [])
                # 과제 문제지가 첨부로만 오는 과목이 있다 (CS311 HW2). 목록만 주면
                # 문제지를 손으로 받아야 하므로 assignments/ 에 바로 받아 둔다.
                if d["attachments"]:
                    saved, new_files = _save_assign_attachments(
                        d.get("course", ""), d["attachments"], base_dir)
                    d["attachment_files"] = saved
                    downloaded.extend(new_files)
                # 링크를 문자열로 흘리면 "이미 받아뒀다" 가 리턴값에 안 남는다.
                d["external_links"] = _resolve_external_links(
                    detail.get("external_links", []),
                    d.get("course", ""),
                    base_dir,
                    fetch=fetch_external,
                )
                for link in d["external_links"]:
                    if link["status"] == "fetched":
                        external_fetched += 1
                    elif link["status"] == "error":
                        warnings.append(
                            f"외부 자료 받기 실패 ({link['url']}): {link['error']}"
                        )
            else:
                warnings.append(f"과제 본문 조회 실패 ({d.get('name')}): {detail.get('error')}")
    else:
        warnings.append(f"마감 조회 실패: {dd.get('error')}")
        fetch_failed = True

    # KLMS 활동이 없는 과목은 일정이 공지 본문에만 있다. 이미 받아둔 공지를 읽는다.
    covered = [(d.get("course"), d.get("due_date_iso") or "", d.get("type") or "")
               for d in deadlines]
    from_notices = [
        h for h in _schedule_from_notices(base_dir, days_ahead, course_names=_course_names())
        if not any(_same_deadline(h, c) for c in covered)
    ]
    for h in from_notices:
        h["local_artifacts"] = _local_artifacts(h["course"], h["name"], base_dir)
        # EE202 처럼 제출처가 KLMS 밖인 과목은 마감이 공지 본문에만 있다.
        # 'assign' 이 아니라는 이유로 미제출 집계에서 통째로 빠지면 안 된다.
        h["external_submit"] = (
            not _is_exam(h["name"])
            and bool(_asks_for_a_submission(h["name"])
                     or _asks_for_a_submission(h["due_date"]))
            and not _already_submitted(h["course"], h["due_date_iso"])
        )
    deadlines = sorted(deadlines + from_notices,
                       key=lambda d: d.get("due_date_iso") or "9999")

    # 조회가 실패했으면 왜 실패했는지까지 말해준다. 숫자만 0 으로 돌려주면
    # '변화 없음'과 구별되지 않는다 (실측 2026-09-08: 세 갈래가 전부 타임아웃났는데
    # 요약은 "새 파일 0개, 공지 0건"이었다). 경고가 있을 때만 한 번 짚어본다.
    if fetch_failed:
        try:
            _, _, locked = _probe()
            if locked:
                warnings.insert(0, _LOCK_HINT)
        except Exception:
            pass  # 원인 진단이 실패해도 이미 모은 결과를 잃으면 안 된다

    unsubmitted = [
        d for d in deadlines
        if d.get("type") == "assign"
        and (any(w in (d.get("submission_status") or "") for w in _NOT_SUBMITTED) or d.get("restricted"))
    ]

    external_submit = [d for d in deadlines if d.get("external_submit")]

    # 4. 성적: 알림에 안 뜨니 성적부를 직접 읽어 state 에 쌓는다.
    new_grades: list[dict] = []
    gr = get_grades()
    if gr.get("success"):
        warnings.extend(f"성적부: {e}" for e in gr.get("errors", []))
        try:
            new_grades = _record_grades(gr.get("grades", []))
        except Exception as e:
            warnings.append(f"성적 기록 실패: {e}")
    else:
        warnings.append(f"성적부 조회 실패: {gr.get('error')}")

    summary = {
        "new_files": len(downloaded),
        "updated_files": len(updated),
        "new_notices": len(notices),
        "deadlines": len(deadlines),
        "unsubmitted": len(unsubmitted),
        "external_submit": len(external_submit),
        "notice_schedule": len(from_notices),
        "external_fetched": external_fetched,
        "new_grades": len(new_grades),
        "warnings": len(warnings),
    }
    bits = [
        f"새 파일 {summary['new_files']}개",
        f"갱신 {summary['updated_files']}개",
        f"공지 {summary['new_notices']}건",
        f"마감 {summary['deadlines']}건(미제출 {summary['unsubmitted']}"
        + (f", 외부 제출 {summary['external_submit']}" if external_submit else "")
        + ")",
    ]
    if from_notices:
        bits.append(f"공지에서 읽은 일정 {len(from_notices)}건")
    if external_fetched:
        bits.append(f"외부 자료 {external_fetched}개")
    if new_grades:
        bits.append(f"새 성적 {len(new_grades)}건")
    if warnings:
        bits.append(f"경고 {len(warnings)}건")

    return _ok({
        "summary": summary,
        "downloaded": downloaded,
        "updated": updated,
        "notices": notices,
        "deadlines": deadlines,
        "new_grades": new_grades,
        "warnings": warnings,
        "message": ", ".join(bits),
    })


def submit_assignment(
    file_path: str,
    assign_url: str,
    comment: str = "",
) -> dict:
    """
    KLMS 과제에 파일을 제출합니다.

    흐름:
      1. GET editsubmission 페이지 → draft itemid / client_id 추출
      2. POST repository_ajax.php?action=upload → 파일을 draft 영역에 업로드
      3. POST savesubmission → 제출 확정

    Args:
        file_path:  업로드할 파일 경로
        assign_url: 과제 URL (예: https://klms.kaist.ac.kr/mod/assign/view.php?id=1231535)
        comment:    제출 코멘트 (선택)

    Returns:
        {"success": True, "file": str, "submitted_at": str, "message": str}
    """
    try:
        fpath = Path(file_path)
        if not fpath.exists():
            return _err(f"파일 없음: {fpath}")

        session = _make_session()

        # ── 1. 세션 확인 ───────────────────────────────────────────────
        r0 = session.get(assign_url, timeout=HTTP_TIMEOUT)
        if not _is_session_valid(r0.url, r0.text):
            return _err("세션 만료 — login()을 먼저 실행하세요")

        # ── 2. sesskey ─────────────────────────────────────────────────
        sesskey = _get_sesskey(session)
        if not sesskey:
            return _err("sesskey 추출 실패")

        # ── 3. 편집 제출 페이지 GET ────────────────────────────────────
        sep = "&" if "?" in assign_url else "?"
        edit_url = assign_url + sep + "action=editsubmission"

        time.sleep(FETCH_DELAY)
        er = session.get(edit_url, timeout=HTTP_TIMEOUT)

        fp = _extract_fp_data(er.text)
        itemid = fp.get("itemid")
        client_id = fp.get("client_id", "")
        fm_field = fp.get("fm_field", "files_filemanager")
        upload_repo_id = fp.get("upload_repo_id", "")

        if not itemid:
            # 디버그용: 페이지 일부 반환
            snippet = er.text[:2000]
            return _err(
                f"draft itemid 추출 실패.\n"
                f"edit_url: {edit_url}\n"
                f"page_snippet: {snippet}"
            )

        if not client_id:
            import random
            client_id = str(random.randint(10**9, 10**10 - 1))

        # ── 4. 파일 업로드 (repository AJAX) ──────────────────────────
        time.sleep(FETCH_DELAY)
        file_bytes = fpath.read_bytes()
        mime = _guess_mime(fpath.name)

        upload_data = {
            "sesskey": sesskey,
            "client_id": client_id,
            "itemid": itemid,
            "author": "",
            "title": fpath.name,
            "savepath": "/",
            "env": "filemanager",
            "p": "",
            "page": "",
        }
        if upload_repo_id:
            upload_data["repo_id"] = upload_repo_id

        up_resp = session.post(
            f"{KLMS_BASE}/repository/repository_ajax.php",
            params={"action": "upload"},
            data=upload_data,
            files={"repo_upload_file": (fpath.name, file_bytes, mime)},
            timeout=120,
        )

        if up_resp.status_code != 200:
            return _err(f"파일 업로드 실패: HTTP {up_resp.status_code}\n{up_resp.text[:500]}")

        try:
            up_json = up_resp.json()
        except Exception:
            return _err(f"업로드 응답 파싱 실패: {up_resp.text[:300]}")

        if "error" in up_json:
            return _err(f"업로드 오류: {up_json['error']}")

        # ── 5. 제출 저장 ───────────────────────────────────────────────
        time.sleep(FETCH_DELAY)

        # 편집 폼의 hidden input 전부 수집 (courseid, id 등 필요 필드 포함)
        esoup = BeautifulSoup(er.text, "html.parser")
        form = esoup.select_one("form#mform1") or esoup.find("form")
        hidden = {}
        if form:
            for inp in form.find_all("input", {"type": "hidden"}):
                n, v = inp.get("name", ""), inp.get("value", "")
                if n:
                    hidden[n] = v

        save_data = {
            **hidden,
            "sesskey": sesskey,
            "action": "savesubmission",
            "submitbutton": "변경사항 저장",
            fm_field: itemid,
        }
        if comment:
            save_data["assignsubmission_comments_editor[text]"] = comment
            save_data["assignsubmission_comments_editor[format]"] = "1"

        form_action = assign_url
        if form:
            fa = form.get("action", "")
            if fa:
                form_action = _norm_url(fa) if fa.startswith("/") else fa

        save_resp = session.post(form_action, data=save_data, timeout=30, allow_redirects=True)

        # ── 6. 결과 확인 ───────────────────────────────────────────────
        ok_signals = ("제출됨", "Submitted", "submissionstatus_submitted", "submissionstatussubmitted")
        confirmed = any(s in save_resp.text for s in ok_signals)

        return _ok({
            "submitted": confirmed,
            "file": fpath.name,
            "assign_url": assign_url,
            "submitted_at": datetime.now(tz=timezone.utc).isoformat(),
            "message": f"'{fpath.name}' 제출 완료" if confirmed else f"'{fpath.name}' 제출 요청 완료 (브라우저에서 확인 권장)",
        })

    except Exception as e:
        return _err(str(e))
