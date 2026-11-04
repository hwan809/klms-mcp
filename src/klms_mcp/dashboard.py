"""klms-mcp dashboard: render <dest>/_klms/last_sweep.json as one self-contained HTML page.

No network, no external scripts. The page shows a 3-week deadline timeline by course,
the list of what's due (unsubmitted first in color), recent notices, and new files,
with links into the local folders.
"""

import html
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from klms_mcp import klms

WINDOW_DAYS = 21


def _due(d: dict):
    iso = d.get("due_date_iso")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso[:19]) if "T" in iso else datetime.fromisoformat(iso[:10])
    except ValueError:
        return None


def _kind(d: dict) -> str:
    name = (d.get("name") or "").lower()
    status = d.get("submission_status") or ""
    if d.get("external_submit"):
        return "todo"
    if d.get("type") == "assign":  # '기말 과제'도 과제다. 시험 판정보다 먼저

        # 제출이 확인될 때만 done. 초안, 빈 상태, 오픈 전은 모두 할 일이다.
        low = status.lower()
        done = "제출됨" in status or "submitted for grading" in low
        return "done" if done and not d.get("restricted") else "todo"
    if d.get("type") == "quiz" or klms._is_exam(name):
        return "exam"
    return "other"


def _local(dest: Path, path) -> Path | None:
    """dest 안에 실제로 있는 파일이면 절대 경로로. 상대 경로는 현재 폴더 기준."""
    if not path:
        return None
    p = Path(path).expanduser().resolve()
    return p if p.is_file() and p.is_relative_to(dest) else None


def _rel_link(dest: Path, path: Path) -> str:
    return quote(str(path.relative_to(dest)).replace("\\", "/"))


def _num(s: str):
    """과제 번호: 이름에서 처음 나오는 숫자. 'Assignment 4 (Due Nov 3)' -> '4'."""
    m = re.search(r"\d+", s)
    return m.group(0) if m else None


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9가-힣]+", s.lower()) if len(w) > 1 or w.isdigit()}


def _workspace(dest: Path, d: dict):
    """이 마감에 맞는 START_HERE.md 를 찾는다. 과목 폴더 안에서 이름이 가장 많이 겹치는 것."""
    course_dir = klms._course_dest_dir(d.get("course", ""), dest)
    if not course_dir.is_dir():
        return None
    want = _words(d.get("name", ""))
    best, score = None, 0
    for p in course_dir.rglob("START_HERE.md"):
        have = _words(p.parent.name)
        # 번호가 둘 다 있는데 다르면 다른 과제다 ('Assignment 4' 와 'Assignment 3').
        wn, hn = _num(d.get("name", "")), _num(p.parent.name)
        if wn and hn and wn != hn:
            continue
        common = want & have
        # 숫자만 겹치면 다른 일이다 ('Quiz 4' 와 'Assignment 4').
        s = len(common) if any(not w.isdigit() for w in common) else 0
        if s > score:
            best, score = p, s
    return best


def _recent_notices(dest: Path, limit: int = 6) -> list[tuple[str, str, Path]]:
    found = []
    for p in dest.glob("*/notices/notice_*.md"):
        m = re.match(r"notice_(\d{4}-\d{2}-\d{2})_(.+)\.md$", p.name)
        if not m:
            continue
        title = m.group(2).replace("-", " ")
        try:  # 파일 첫 줄의 제목이 더 정확하다
            for line in p.read_text(encoding="utf-8").splitlines()[:5]:
                if line.startswith("# "):
                    title = line[2:].strip() or title
                    break
        except OSError:
            pass
        found.append((m.group(1), title, p))
    found.sort(key=lambda x: x[0], reverse=True)
    return found[:limit]


def _md(d) -> str:
    """'Nov 4'. strftime 의 %-d 는 Windows 에 없다."""
    return f"{d.strftime('%b')} {d.day}"


def _fmt_due(dt: datetime, today: date) -> tuple[str, str]:
    days = (dt.date() - today).days
    rel = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days" if days > 1 else f"{-days} days ago"
    when = dt.strftime("%a ") + _md(dt) + ("" if (dt.hour, dt.minute) == (0, 0) else dt.strftime(", %H:%M"))
    return when, rel


_PROBLEM_WORDS = ("실패", "잠금", "오류", "error", "timeout", "timed out", "파일 다운로드:", "성적부:")


def save(dest: Path, result: dict) -> tuple[Path, Path]:
    """sweep 결과를 <dest>/_klms/last_sweep.json 에 쓰고 대시보드를 다시 그린다."""
    dest = Path(dest).expanduser().resolve()
    out = dest / "_klms" / "last_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out, render(dest)


def render(dest: Path, today: date | None = None) -> Path:
    dest = Path(dest).expanduser().resolve()
    data_path = dest / "_klms" / "last_sweep.json"
    if not data_path.is_file():
        raise SystemExit(f"{data_path} 가 없습니다. 먼저 klms-mcp sweep --dest {dest}")
    r = json.loads(data_path.read_text(encoding="utf-8"))
    today = today or date.today()
    end = today + timedelta(days=WINDOW_DAYS)
    e = html.escape

    items, late = [], []  # late: 지났거나 날짜를 못 읽은 미제출 과제. 타임라인엔 없고 목록 맨 위에.
    for d in r.get("deadlines", []):
        dt, k = _due(d), _kind(d)
        if dt is not None and today <= dt.date() <= end:
            items.append((dt, d, k))
        elif k == "todo" and d.get("type") == "assign" and (dt is None or dt.date() < today):
            late.append((dt, d, k))
    items.sort(key=lambda x: x[0])
    late.sort(key=lambda x: x[0] or datetime.max)
    courses = sorted({d.get("course", "") for _, d, _ in items})

    # ── timeline (inline SVG) ──
    W, left, row = 1000, 230, 34
    H = 46 + row * max(len(courses), 1)
    x = lambda dt: left + (dt - datetime.combine(today, datetime.min.time())).total_seconds() / (WINDOW_DAYS * 86400) * (W - left - 20)
    svg = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Deadline timeline">']
    for i in range(0, WINDOW_DAYS + 1):
        day = today + timedelta(days=i)
        xi = x(datetime.combine(day, datetime.min.time()))
        weekend = day.weekday() >= 5
        if weekend and i < WINDOW_DAYS:
            svg.append(f'<rect x="{xi:.1f}" y="30" width="{(W-left-20)/WINDOW_DAYS:.1f}" height="{H-30}" class="wknd"/>')
        if day.weekday() == 0 or i == 0:
            svg.append(f'<line x1="{xi:.1f}" y1="30" x2="{xi:.1f}" y2="{H}" class="grid"/>')
            svg.append(f'<text x="{xi+3:.1f}" y="20" class="tick">{_md(day)}</text>')
    tx = x(datetime.now()) if today == date.today() else x(datetime.combine(today, datetime.min.time()))
    svg.append(f'<line x1="{tx:.1f}" y1="26" x2="{tx:.1f}" y2="{H}" class="now"/>')
    for ci, c in enumerate(courses):
        y = 46 + ci * row
        svg.append(f'<text x="{left-12}" y="{y+5}" class="course" text-anchor="end">{e(c[:30])}</text>')
        svg.append(f'<line x1="{left}" y1="{y}" x2="{W-20}" y2="{y}" class="lane"/>')
        for dt, d, k in items:
            if d.get("course", "") == c:
                svg.append(f'<circle cx="{x(dt):.1f}" cy="{y}" r="7" class="dot {k}"><title>{e(d.get("name",""))}: {e(_md(dt) + dt.strftime(" %H:%M"))}</title></circle>')
    if not courses:
        svg.append(f'<text x="{left}" y="56" class="tick">Nothing due in the next {WINDOW_DAYS} days.</text>')
    svg.append("</svg>")

    # ── due list ──
    label = {"todo": "To do", "exam": "Exam / quiz", "done": "Submitted", "other": "Scheduled"}
    rows = []
    for dt, d, k in late + items:
        when, rel = _fmt_due(dt, today) if dt else (d.get("due_date") or "", "no date")
        if dt and dt.date() < today:
            rel = "overdue, " + rel
        # 링크는 이 폴더에 받아 둔 파일로 건다. 로컬 파일이 없을 때만 KLMS 원문.
        links = []
        ws = _workspace(dest, d)
        if ws:
            links.append(f'<a href="{_rel_link(dest, ws)}">START_HERE.md</a>')
        note = _local(dest, d.get("source_notice"))
        if note:
            links.append(f'<a href="{_rel_link(dest, note)}">notice: {e(d.get("notice_title") or note.stem)}</a>')
        if not links and (d.get("url") or "").startswith("https://klms.kaist.ac.kr"):
            links.append(f'<a href="{e(d["url"])}">KLMS</a>')
        rows.append(
            f'<li class="item {k}"><div class="when"><b>{e(rel)}</b><span>{e(when)}</span></div>'
            f'<div class="what"><span class="pill {k}">{label[k]}</span><b>{e(d.get("name",""))}</b>'
            f'<span class="sub">{e(d.get("course",""))}</span></div>'
            f'<div class="links">{" ".join(links)}</div></li>')

    notices = "".join(
        f'<li><span class="date">{e(day)}</span><a href="{_rel_link(dest, p)}">{e(t)}</a>'
        f'<span class="sub">{e(p.parent.parent.name)}</span></li>'
        for day, t, p in _recent_notices(dest)) or "<li class='sub'>No notices saved yet.</li>"

    new_files = r.get("downloaded", [])
    files = "".join(
        f'<li><a href="{_rel_link(dest, lp)}">{e(f.get("filename",""))}</a><span class="sub">{e(f.get("course",""))}</span></li>'
        for f in new_files[:12] if (lp := _local(dest, f.get("saved_to"))))
    more = f"<li class='sub'>and {len(new_files)-12} more</li>" if len(new_files) > 12 else ""

    s = r.get("summary", {})
    todo = sum(1 for *_, k in items if k == "todo") + len(late)
    exams = sum(1 for *_, k in items if k == "exam")
    # 안내성 메모('외부 도구 2개' 등)는 빼고 실제로 실패한 점검만 띄운다.
    warns = [str(w) for w in r.get("warnings") or []
             if any(k in str(w).lower() for k in _PROBLEM_WORDS)]
    warn_html = ""
    if warns:
        lis = "".join(f"<li>{e(w)}</li>" for w in warns[:8])
        warn_html = (f'<section class="card warn"><h2>Some checks failed, so this page may be incomplete</h2>'
                     f'<ul>{lis}</ul><p class="sub">Run the sweep again. If KLMS asks you to log in, run klms-mcp login.</p></section>')
    mt = datetime.fromtimestamp(data_path.stat().st_mtime)
    stamp = mt.strftime("%a ") + _md(mt) + mt.strftime(", %H:%M")

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KLMS Dashboard</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--sub:#6e6e73;--line:#e3e3e0;--todo:#e5484d;--exam:#f5a524;--done:#30a46c;--other:#7c8db5;--now:#1d1d1f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141415;--card:#1d1d1f;--fg:#f2f2f2;--sub:#a1a1a6;--line:#333336;--now:#f2f2f2}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:28px 16px 48px}}
header{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
h1{{font-size:26px;margin:0}}h2{{font-size:16px;margin:0 0 12px}}.sub{{color:var(--sub);font-size:13px}}
.stats{{display:flex;gap:10px;margin:18px 0;flex-wrap:wrap}}.stat{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 16px;min-width:120px}}
.stat b{{display:block;font-size:24px}}.stat.todo b{{color:var(--todo)}}.stat.exam b{{color:var(--exam)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}}
.tl{{overflow-x:auto}}svg{{width:100%;min-width:680px;height:auto;display:block}}.tick{{font-size:12px;fill:var(--sub)}}.course{{font-size:13px;fill:var(--fg)}}
.grid{{stroke:var(--line)}}.lane{{stroke:var(--line);stroke-dasharray:2 4}}.wknd{{fill:var(--line);opacity:.35}}.now{{stroke:var(--now);stroke-width:1.5;stroke-dasharray:4 3}}
.dot{{stroke:var(--card);stroke-width:2}}.dot.todo{{fill:var(--todo)}}.dot.exam{{fill:var(--exam)}}.dot.done{{fill:var(--done)}}.dot.other{{fill:var(--other)}}
.legend{{display:flex;gap:14px;margin-top:8px;flex-wrap:wrap}}.legend span::before{{content:"";display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;background:var(--c)}}
ul{{list-style:none;margin:0;padding:0}}.item{{display:grid;grid-template-columns:130px 1fr auto;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}}
.item:first-child{{border-top:0}}.when b{{display:block}}.when span{{color:var(--sub);font-size:13px}}.what b{{display:block}}
.pill{{display:inline-block;font-size:11px;font-weight:600;padding:1px 8px;border-radius:99px;margin-bottom:2px;color:#fff;background:var(--other)}}
.pill.todo{{background:var(--todo)}}.pill.exam{{background:var(--exam)}}.pill.done{{background:var(--done)}}
.item.todo .when b{{color:var(--todo)}}a{{color:inherit;text-decoration:underline;text-underline-offset:2px}}.links{{max-width:300px;text-align:right}}.links a{{margin-left:10px;font-size:13px}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.cols li{{padding:6px 0;border-top:1px solid var(--line);display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}.cols li:first-child{{border-top:0}}
.date{{color:var(--sub);font-size:13px;min-width:84px}}
.warn{{border-color:var(--exam)}}.warn li{{padding:3px 0;font-size:14px}}
@media (max-width:700px){{.stat{{min-width:0;flex:1 1 40%}}.cols{{grid-template-columns:1fr}}.item{{grid-template-columns:1fr}}.links{{max-width:none;text-align:left}}.links a{{margin:0 10px 0 0}}}}
</style></head><body><main>
<header><h1>KLMS</h1><span class="sub">Updated {e(stamp)} by klms-mcp</span></header>
{warn_html}<div class="stats">
<div class="stat todo"><b>{todo}</b>to do</div>
<div class="stat exam"><b>{exams}</b>exams and quizzes</div>
<div class="stat"><b>{len(items)}</b>due in {WINDOW_DAYS} days</div>
<div class="stat"><b>{s.get("new_files", 0)}</b>new files</div>
<div class="stat"><b>{s.get("new_notices", 0)}</b>new notices</div>
</div>
<section class="card"><h2>Next {WINDOW_DAYS} days</h2><div class="tl">{"".join(svg)}</div>
<div class="legend sub"><span style="--c:var(--todo)">to do</span><span style="--c:var(--exam)">exam or quiz</span><span style="--c:var(--done)">submitted</span><span style="--c:var(--other)">other</span><span>dashed line: now</span></div></section>
<section class="card"><h2>What's due</h2><ul>{"".join(rows) or "<li class='sub'>Nothing due.</li>"}</ul></section>
<div class="cols">
<section class="card"><h2>Recent notices</h2><ul>{notices}</ul></section>
<section class="card"><h2>New files</h2><ul>{files or "<li class='sub'>No new files in the last check.</li>"}{more}</ul></section>
</div>
</main></body></html>
"""
    out = dest / "dashboard.html"
    out.write_text(page, encoding="utf-8")
    return out
