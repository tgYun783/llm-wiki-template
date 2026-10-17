#!/usr/bin/env python3
"""LLM 위키 규칙 검사기 (CLAUDE.md의 기계적으로 확인 가능한 규칙).

사용:
  python3 .claude/wiki_lint.py          # 사람이 읽는 보고서. 오류가 있으면 exit 1
  python3 .claude/wiki_lint.py --hook   # Stop hook 모드 (stdin: hook JSON)
  python3 .claude/wiki_lint.py --session-start  # SessionStart hook: 세션 시작 시점 스냅샷
  python3 .claude/wiki_lint.py --review-done    # SubagentStop hook: wiki-reviewer 의미 검토 영수증
  python3 .claude/wiki_lint.py --pre-tool       # PreToolUse hook: raw/ 기존 파일 수정·하위 폴더 생성 거부
  python3 .claude/wiki_lint.py --status SID     # 상태 JSON (읽기 전용, 상태 파일을 쓰지 않음 — 상태 표시 mod용)

git (로컬 전용): 세션 시작 때 사용자 변경을 따로 커밋하고, Stop hook을 통과하면 이 세션 작업을 커밋한다.

ERROR  = 규칙 위반. 검증 루프에서 0이 될 때까지 고친다.
WARN   = 판단이 필요한 항목. 보고만 하고 루프를 막지 않는다.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI = os.path.join(ROOT, "wiki")
RAW = os.path.join(ROOT, "raw")
STATE = os.path.join(ROOT, ".claude", ".lint_state.json")
META = ("index", "log")
MAX_LOOPS = 5
REVIEWER = "wiki-reviewer"
# entity 생성 기준은 소급 적용하지 않는다 (.claude/rules/페이지-형식.md): 검사 도입(2026-09-24) 전부터 있던 entity는 경고만
ENTITY_GRANDFATHERED = set()
LOG_HEADER = re.compile(r"^## \[\d{4}-\d{2}-\d{2}\] (ingest|query|lint|schema) \| .+$")
NUM = re.compile(r"\d+\.\d+%|\d+%|\$\d|\d(\.\d)?배")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def split_fm(text):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5:]


def domains_from_schema():
    m = re.search(r"현재 도메인: (.+)", read(os.path.join(ROOT, "CLAUDE.md")))
    return set(re.findall(r"도메인/([^`,\s]+)", m.group(1))) if m else set()


def links(text):
    text = re.sub(r"`[^`]*`", "", text)
    return set(l.strip() for l in re.findall(r"\[\[([^\]|#]+)", text))


def unprocessed_raw(pages):
    """CLAUDE.md 건강검진 6번: 어떤 source 페이지의 `raw:`로도 연결되지 않은 raw 파일.
    첨부(이미지·PDF 등)는 log를 뺀 wiki 본문에서 파일명이 언급되면 처리된 것으로 본다."""
    linked = set()
    for t in pages.values():
        fm, _ = split_fm(t)
        if fm and re.search(r"^type: source", fm, re.M):
            linked.update(re.findall(r'^raw: "(.+)"', fm, re.M))
    body = "\n".join(t for k, t in pages.items() if k != "log")
    out = []
    for n in sorted(os.listdir(RAW)):
        if n.startswith(".") or n in linked:
            continue
        if not n.endswith((".md", ".txt")) and n in body:
            continue
        out.append(n)
    return out


def norm_quote(t):
    """인용 대조용 정규화: 마크다운 강조 기호·따옴표 종류·공백·대소문자 차이를 무시한다."""
    t = re.sub(r"[*`_]", "", t)
    t = re.sub(r"[\"'‘’“”]", "'", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def lint(log_base=None):
    """log_base: (길이, sha) — append-only·검증 줄 검사의 기준점. 없으면 마지막 clean 시점."""
    errors, warns = [], []
    E = lambda *a: errors.append(" ".join(str(x) for x in a))
    W = lambda *a: warns.append(" ".join(str(x) for x in a))

    for d in (WIKI, RAW):
        for n in os.listdir(d):
            if os.path.isdir(os.path.join(d, n)):
                E("[하위폴더]", os.path.relpath(os.path.join(d, n), ROOT))

    pages = {}
    for n in sorted(os.listdir(WIKI)):
        if n.endswith(".md"):
            pages[n[:-3]] = read(os.path.join(WIKI, n))
        elif not n.startswith("."):
            W("[wiki 비마크다운 파일]", n)
    known_domains = domains_from_schema()

    # 1. 필수 형식
    for k, t in pages.items():
        fm, body = split_fm(t)
        if fm is None:
            E("[frontmatter 없음]", k)
            continue
        if not body.lstrip("\n").startswith("> [!abstract] 핵심 한 줄"):
            E("[핵심 한 줄 callout이 본문 맨 앞에 없음]", k)
        for key in ("type", "created", "updated"):
            if not re.search(rf"^{key}:", fm, re.M):
                E("[frontmatter 누락]", k, key)
        if re.search(r"[A-Z\s_]", k):
            E("[wiki 파일명 규칙 위반: 케밥케이스 아님]", k)
        if k in META:
            continue
        m = re.search(r"^tags: \[(.*)\]$", fm, re.M)
        tags = [x.strip() for x in m.group(1).split(",")] if m else []
        doms = {x.split("/", 1)[1] for x in tags if x.startswith("도메인/")}
        if not doms:
            E("[도메인 태그 없음]", k)
        for d in doms - known_domains:
            E("[CLAUDE.md에 없는 도메인]", k, d, "→ 새 도메인은 사용자 확인 후 CLAUDE.md 목록에 추가")
        for x in tags:
            if x in doms:
                E("[도메인과 같은 이름의 일반 태그]", k, x)
        if re.search(r"^type: source", fm, re.M):
            if not k.startswith("요약-"):
                E("[source 페이지에 요약- 접두사 없음]", k)
            for key in ("raw", "source_url", "author"):
                if not re.search(rf"^{key}:", fm, re.M):
                    (W if key == "source_url" else E)("[source frontmatter 누락]", k, key)
            rm = re.search(r'^raw: "(.+)"', fm, re.M)
            if rm and not os.path.exists(os.path.join(RAW, rm.group(1))):
                E("[source의 raw 파일이 없음]", k, rm.group(1))
        if re.search(r"^- \[ \] ", t, re.M) and not re.search(r"^type: project", fm, re.M):
            E("[project 외 페이지에 체크박스]", k)
        if re.search(r"^type: project", fm, re.M):
            sm = re.search(r"^status: (.+)$", fm, re.M)
            if not sm or sm.group(1).strip() not in ("진행중", "보류", "완료"):
                E("[project status 누락/형식 오류]", k)
            rp = re.search(r'^repo: "(.+)"', fm, re.M)
            if rp and not os.path.isdir(rp.group(1)):
                E("[project repo 경로 없음]", k, rp.group(1))

    for n in sorted(os.listdir(RAW)):
        if n.startswith("."):
            continue
        # 날짜 접두사만 필수. 뒤는 Web Clipper 제목 그대로 허용 (CLAUDE.md §2)
        if not re.match(r"^\d{4}-\d{2}-\d{2}_.+\.\w+$", n):
            W("[raw 파일명 규칙 위반]", n, "(raw는 Claude가 고치지 않음 — 사용자에게 알림)")

    # 2·3. 고아 / 빨간 링크
    inbound = {k: set() for k in pages}
    for k, t in pages.items():
        for l in links(t):
            if l in pages:
                if l != k:
                    inbound[l].add(k)
            elif k != "log" and not os.path.isfile(os.path.join(RAW, l)):  # raw 첨부 임베드는 링크 대상
                W("[빨간 링크]", k, "→", l)
    for k, src in inbound.items():
        if k not in META and not (src - set(META)):
            E("[고아 페이지]", k)

    # 4. index
    idx = pages.get("index", "")
    for k in pages:
        if k not in META and f"[[{k}]]" not in idx:
            E("[index 누락]", k)
    for l in links(idx):
        if l not in pages:
            E("[index에 없는 페이지 항목]", l)
    content = [k for k in pages if k not in META]
    cm = re.search(r"페이지 수: (\d+)", idx)
    if cm and int(cm.group(1)) != len(content):
        E("[index 페이지 수 불일치]", cm.group(1), "≠", len(content))
    sm = re.search(r"자료 수: (\d+)", idx)
    n_src = len([k for k in content if k.startswith("요약-")])
    if sm and int(sm.group(1)) != n_src:
        E("[index 자료 수 불일치]", sm.group(1), "≠", n_src)

    # 5. 시점 표시 (휴리스틱이라 WARN)
    for k in content:
        _, body = split_fm(pages[k])
        # source 페이지의 `## 기본 정보` 제목에 붙은 기준 표시는 페이지 전체에 적용 (.claude/rules/페이지-형식.md)
        page_wide = k.startswith("요약-") and re.search(r"^## 기본 정보.*\(기준:", body, re.M)
        sec = False
        for line in body.split("\n"):
            if line.startswith("#"):
                sec = "(기준:" in line
                continue
            if line.startswith(">") or page_wide or sec or "(기준:" in line:
                continue
            if NUM.search(line):
                W("[시점 표시 없는 수치?]", k, "|", line.strip()[:70])

    # 7. 인용문 원문 대조 (의미 검토 3번 중 기계로 되는 부분): `> "인용" — 출처` 형식은 raw에 그대로 있어야 한다
    raw_text = norm_quote("\n".join(
        read(os.path.join(RAW, n)) for n in os.listdir(RAW) if n.endswith((".md", ".txt"))))
    for k in content:
        for m in re.finditer(r'^> "(.+?)"\s+—', pages[k], re.M):
            for seg in re.split(r"…|\.\.\.", m.group(1)):  # 생략 부호로 나눈 조각마다 대조
                if len(seg.strip()) >= 8 and norm_quote(seg) not in raw_text:
                    E("[raw에 없는 인용문]", k, "|", seg.strip()[:60], "→ 원문 그대로 인용하거나, 번역·요약이면 인용 형식을 쓰지 않는다")
                    break

    # 8. entity 생성 기준 (의미 검토 6번 중 기계로 되는 부분, .claude/rules/페이지-형식.md)
    for k in content:
        fm, _ = split_fm(pages[k])
        if fm and re.search(r"^type: entity", fm, re.M):
            sm = re.search(r"^sources: \[(.*)\]", fm, re.M)
            n_src = len(re.findall(r"\[\[", sm.group(1))) if sm else 0
            n_in = len(inbound[k] - set(META))
            if n_src < 2 and n_in < 2:
                (W if k in ENTITY_GRANDFATHERED else E)("[entity 생성 기준 미달]", k, f"자료 {n_src}건 · 들어오는 링크 {n_in}곳",
                  "→ 상위 페이지의 섹션으로 두고 섹션 링크를 건다")

    # 6. 미처리 raw (사용자가 넣고 아직 넣기 전일 수 있어 WARN)
    for n in unprocessed_raw(pages):
        W("[미처리 raw]", n)

    # log 형식 + append-only
    log = pages.get("log", "")
    for line in log.split("\n"):
        if line.startswith("## [") and not LOG_HEADER.match(line):
            E("[log 헤더 형식 위반]", line[:60])
    if log_base is None:
        state = load_state()
        log_base = state.get("log_prefix_len", 0), state.get("log_prefix_sha")
    base_len, base_sha = log_base
    if base_sha:
        if hashlib.sha256(log[:base_len].encode()).hexdigest() != base_sha:
            E("[log.md 기존 내용이 수정됨 — append-only 위반]")
        else:
            # 새로 추가한 log 항목마다 검증 결과 줄 필수 (CLAUDE.md §6)
            for entry in re.split(r"\n(?=## \[)", log[base_len:]):
                head = entry.lstrip("\n").split("\n", 1)[0]
                if head.startswith("## [") and not re.search(r"^- 검증: .*의미 검토", entry, re.M):
                    E("[log 항목에 검증 줄 없음]", head[:60], "→ `- 검증: 기계 검사 … · 의미 검토 N회차 …` 추가")

    return errors, warns, log


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def wiki_fingerprint():
    h = hashlib.sha256()
    for d in (WIKI, RAW):
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                h.update(n.encode())
                h.update(str(os.path.getmtime(p)).encode())
    h.update(str(os.path.getmtime(os.path.join(ROOT, "CLAUDE.md"))).encode())
    return h.hexdigest()


def page_hashes():
    """log.md를 뺀 wiki 페이지별 내용 해시. 의미 검토 영수증과 '이 세션이 바꾼 페이지' 판단에 쓴다.
    log.md는 검토가 끝난 뒤 검증 줄을 적는 곳이라 제외한다."""
    out = {}
    for n in sorted(os.listdir(WIKI)):
        if n.endswith(".md") and n != "log.md":
            out[n] = sha(read(os.path.join(WIKI, n)))
    return out


def content_fp(hashes):
    return sha(json.dumps(hashes, sort_keys=True))


def raw_hashes():
    """raw/ 파일별 내용 해시 (이미지·PDF 포함). 세션 중 기존 raw가 바뀌었는지 판단한다 (절대 규칙 1)."""
    out = {}
    for n in sorted(os.listdir(RAW)):
        p = os.path.join(RAW, n)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                out[n] = hashlib.sha256(f.read()).hexdigest()
    return out


def raw_changes(before):
    """세션 시작 때 있던 raw 파일 중 바뀌거나 사라진 것. 새로 생긴 파일은 넣기 1단계·녹음 가져오기 등 정상 입력이라 뺀다."""
    now = raw_hashes()
    return ([f"수정: raw/{n}" for n in sorted(before) if n in now and now[n] != before[n]]
            + [f"삭제·이동: raw/{n}" for n in sorted(before) if n not in now])


def pre_tool(hook_in):
    """PreToolUse: 파일 쓰기 도구가 raw/의 기존 파일을 고치거나 raw/ 안에 하위 폴더를 만들려 하면 거부한다.
    새 파일 저장(넣기 1단계)은 허용한다. Bash 경유 변경은 여기서 못 잡으니 Stop hook이 사후에 잡는다."""
    ti = hook_in.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    if not path:
        sys.exit(0)
    raw = os.path.realpath(RAW)
    p = os.path.realpath(path if os.path.isabs(path) else os.path.join(hook_in.get("cwd") or ROOT, path))
    if not p.startswith(raw + os.sep):
        sys.exit(0)
    why = None
    if os.path.dirname(p) != raw:
        why = "raw/ 안에 하위 폴더를 만들 수 없다 (절대 규칙 2). raw/에 평평하게 `YYYY-MM-DD_설명` 파일로 둔다."
    elif os.path.lexists(p):
        why = ("raw/는 불변이다 (절대 규칙 1). 기존 raw 파일은 수정·덮어쓰기할 수 없다. "
               "오타·파일명 문제는 사용자에게 알리기만 하라.")
    if why:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": f"[위키 raw 보호] {why} ({os.path.relpath(p, ROOT)})"}}, ensure_ascii=False))
    sys.exit(0)


def template_stale():
    """공개 템플릿 동기화 스크립트(.claude/sync_template.py)가 있으면, 템플릿에 반영 안 된 시스템 파일 목록."""
    path = os.path.join(ROOT, ".claude", "sync_template.py")
    if not os.path.exists(path):
        return []
    try:
        r = subprocess.run([sys.executable, path, "--check"], capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    return r.stdout.split()


def git(*args):
    """볼트의 로컬 git. 저장소가 아니거나 실패하면 None."""
    try:
        r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def git_dirty():
    out = git("status", "--porcelain", "-z", "--untracked-files=all")
    if not out:
        return []
    return [e[3:] for e in out.split("\0") if len(e) > 3]


def git_commit(message):
    """모든 변경을 커밋한다 (로컬 전용, push 안 함). 커밋할 게 없으면 True, 실패하면 False."""
    if git("rev-parse", "--git-dir") is None:
        return True
    if not git_dirty():
        return True
    return git("add", "-A") is not None and git("commit", "-q", "-m", message) is not None


def commit_session_work(prefix=""):
    """Stop 통과 시: 이 세션의 위키 작업을 커밋. 제목은 새로 추가된 마지막 log 항목."""
    diff = git("diff", "HEAD", "--", "wiki/log.md") or ""
    heads = [l[1:].strip() for l in diff.split("\n") if l.startswith("+## [")]
    title = heads[-1][3:] if heads else "위키 작업 저장"
    return git_commit(f"{prefix}{title}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")


def mark_clean(state, log, reset_loops=True):
    state["log_prefix_len"] = len(log)
    state["log_prefix_sha"] = sha(log)
    state["clean_fp"] = wiki_fingerprint()
    if reset_loops:
        state["loops"] = {}
    save_state(state)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def session_start(hook_in):
    """세션 시작 시점의 위키 상태를 기록한다. Stop hook은 이 세션이 만든 오류만 막는다."""
    sid = hook_in.get("session_id", "default")
    state = load_state()
    sessions = state.setdefault("sessions", {})
    if sid in sessions:
        sys.exit(0)  # resume 등으로 같은 세션이 다시 시작되면 처음 스냅샷을 유지
    notes = []
    user_changed = git_dirty()
    if user_changed:
        # 세션 사이에 생긴 변경은 사용자(Obsidian·Web Clipper)가 한 것 — Claude 작업과 섞이지 않게 먼저 따로 커밋
        ok = git_commit("사용자 변경 (세션 시작 시점, Obsidian 등)")
        notes.append(
            f"[위키 git] 지난 커밋 이후 사용자가 바꾼 파일 {len(user_changed)}건"
            + (" (별도 커밋함)" if ok else " (커밋 실패)")
            + ". wiki 페이지를 사용자가 고쳤으면 그 내용을 존중하고 덮어쓰지 마라 (절대 규칙 3).\n- "
            + "\n- ".join(user_changed[:30]))
    errors, _, log = lint()
    sessions[sid] = {
        "fp": wiki_fingerprint(), "pre": errors, "log_len": len(log), "log_sha": sha(log),
        "pages": page_hashes(), "raw": raw_hashes(),
    }
    for old in list(sessions)[:-20]:
        del sessions[old]
    save_state(state)
    if errors:
        notes.append(
            f"[위키 검사기] 이 세션 시작 전부터 규칙 위반 {len(errors)}건이 있다 (사용자가 직접 고친 것일 수 있음). "
            "Stop hook은 이것들로 막지 않는다. 고치지 말고, 위키 작업을 하면 보고에 적어 사용자 확인을 받아라.\n- "
            + "\n- ".join(errors[:20]))
    pages = {n[:-3]: read(os.path.join(WIKI, n)) for n in os.listdir(WIKI) if n.endswith(".md")}
    todo = unprocessed_raw(pages)
    if todo:
        notes.append(
            f"[위키] 아직 넣지 않은 raw 자료 {len(todo)}건 (어떤 요약 페이지의 `raw:`에도 연결 안 됨). "
            "사용자의 첫 요청을 처리한 뒤 한 줄로 \"넣을까요?\"라고 제안하라.\n- " + "\n- ".join(todo[:30]))
    if notes:
        print("\n\n".join(notes))
    sys.exit(0)
    
def last_assistant_text(path):
    """SubagentStop 입력에 last_assistant_message가 없을 때(백그라운드 하위 에이전트), 하위 에이전트
    대화 기록(jsonl)에서 REVIEW-RESULT가 든 마지막 assistant 응답을 찾는다."""
    if not path or not os.path.exists(path):
        return ""
    found = ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "assistant":
                continue
            content = (rec.get("message") or {}).get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            parts = [c.get("text", "") if c.get("type") == "text" else json.dumps(c.get("input", ""), ensure_ascii=False)
                     for c in content if isinstance(c, dict) and c.get("type") in ("text", "tool_use")]
            text = "\n".join(parts).replace("\\n", "\n")
            if "REVIEW-RESULT:" in text:
                found = text
    return found



def review_done(hook_in):
    """SubagentStop: wiki-reviewer가 끝나면 '이 위키 내용에 대해 문제 N건' 영수증을 남긴다."""
    if hook_in.get("agent_type") != REVIEWER:
        sys.exit(0)
    msg = hook_in.get("last_assistant_message") or last_assistant_text(hook_in.get("agent_transcript_path"))
    m = re.search(r"REVIEW-RESULT: issues=(-?\d+)", msg)
    if not m:
        if not hook_in.get("stop_hook_active"):
            print(json.dumps({"decision": "block", "reason": "출력 마지막 줄에 `REVIEW-RESULT: issues=N`을 적어라 (에이전트 정의의 출력 형식)."}, ensure_ascii=False))
        sys.exit(0)
    state = load_state()
    reviews = state.setdefault("reviews", {})
    reviews[content_fp(page_hashes())] = int(m.group(1))
    for old in list(reviews)[:-20]:
        del reviews[old]
    save_state(state)
    sys.exit(0)


def status(sid):
    """상태 표시용 요약을 JSON으로 출력한다. 읽기 전용: 상태 파일·git을 바꾸지 않는다."""
    state = load_state()
    sess = state.get("sessions", {}).get(sid)
    errors, warns, _ = lint((sess["log_len"], sess["log_sha"]) if sess else None)
    pre = set(sess["pre"]) if sess else set()
    review = "none"  # 이 세션이 바꾼 페이지 없음
    if sess and "pages" in sess:
        now = page_hashes()
        if any(sess["pages"].get(n) != now[n] for n in now) or any(n not in now for n in sess["pages"]):
            r = state.get("reviews", {}).get(content_fp(now))
            review = "missing" if r is None else ("ok" if r == 0 else f"issues:{r}")
    pages = {n[:-3]: read(os.path.join(WIKI, n)) for n in os.listdir(WIKI) if n.endswith(".md")}
    print(json.dumps({
        "errors": len(errors), "newErrors": len([e for e in errors if e not in pre]), "warns": len(warns),
        "review": review, "loops": state.get("loops", {}).get(sid, 0), "maxLoops": MAX_LOOPS,
        "unprocessedRaw": len(unprocessed_raw(pages)),
        "rawChanged": len(raw_changes(sess["raw"])) if sess and "raw" in sess else 0,
        "templateStale": len(template_stale()), "session": bool(sess),
    }, ensure_ascii=False))
    sys.exit(0)


def block(state, sid, reason, count):
    loops = state.setdefault("loops", {})
    loops[sid] = loops.get(sid, 0) + 1
    save_state(state)
    if loops[sid] > MAX_LOOPS:
        # 다음 세션이 이 작업을 '사용자 변경'으로 오인하지 않도록, 미통과 표시를 달아 이 세션 작업으로 커밋
        ok = git_commit(f"[검증 미통과 — 사용자 확인 필요] 미해결 {count}건\n\n"
                        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
        print(json.dumps({"systemMessage": f"위키 검증 루프 {MAX_LOOPS}회 후에도 미해결 {count}건 — 사용자 확인 필요"
                          + ("" if ok else " / git 커밋 실패")}, ensure_ascii=False))
        sys.exit(0)
    reason = f"[위키 검증 루프 {loops[sid]}/{MAX_LOOPS}] " + reason
    if loops[sid] == MAX_LOOPS:
        reason += (
            "\n\n이번이 마지막 차단이다. 이번에도 해결하지 못하면 더 시도하지 말고 사용자에게 "
            "① 남은 문제 ② 막힌 원인 ③ 시도한 것 ④ 대안을 보고하고 끝내라."
        )
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    sys.exit(0)


def main():
    if "--status" in sys.argv:
        i = sys.argv.index("--status")
        status(sys.argv[i + 1] if len(sys.argv) > i + 1 else "default")
    hook = "--hook" in sys.argv
    hook_in = {}
    if hook or any(a in sys.argv for a in ("--session-start", "--review-done", "--pre-tool")):
        try:
            hook_in = json.load(sys.stdin)
        except Exception:
            hook_in = {}
    if "--session-start" in sys.argv:
        session_start(hook_in)
    if "--review-done" in sys.argv:
        review_done(hook_in)
    if "--pre-tool" in sys.argv:
        pre_tool(hook_in)
    state = load_state()
    sid = hook_in.get("session_id", "default")
    sess = state.get("sessions", {}).get(sid) if hook else None
    errors, warns, log = lint((sess["log_len"], sess["log_sha"]) if sess else None)

    stale = template_stale()
    if not hook:
        if stale:
            warns.append("[공개 템플릿 미반영] " + ", ".join(stale) + " → `python3 .claude/sync_template.py`")
        for e in errors:
            print("ERROR", e)
        for w in warns:
            print("WARN ", w)
        print(f"\n오류 {len(errors)}건, 경고 {len(warns)}건")
        if not errors:
            mark_clean(state, log)
        sys.exit(1 if errors else 0)

    # --- Stop hook 모드 (Ralph 루프) ---
    # 0) 시스템 파일(규칙·검사기·hook·리뷰어)이 바뀌었는데 공개 템플릿에 반영 안 됨
    if stale:
        block(state, sid,
              "시스템 파일이 공개 템플릿에 반영되지 않았다: " + ", ".join(stale)
              + ". `python3 .claude/sync_template.py`를 실행하고, 유출 의심·예시 자료 오류가 나오면 고친 뒤 다시 실행하세요.",
              len(stale))
    # 0-1) 세션 시작 때 있던 raw 파일이 바뀜 (Bash 경유 등 PreToolUse가 못 잡은 경로). Claude는 되돌릴 수 없으니
    #      (raw 수정·git checkout 금지) 같은 상태로는 한 번만 막아 보고하게 하고, 커밋 제목에 표시를 남긴다.
    raw_changed = raw_changes(sess["raw"]) if sess and "raw" in sess else []
    prefix = "[raw 변경 — 사용자 확인 필요] " if raw_changed else ""
    if raw_changed:
        raw_fp = content_fp(raw_hashes())
        if sess.get("raw_notified") != raw_fp:
            sess["raw_notified"] = raw_fp
            block(state, sid,
                  f"이 세션 동안 기존 raw 파일 {len(raw_changed)}건이 바뀌었다 (절대 규칙 1: raw/는 불변). "
                  "직접 되돌리지 마라 (raw 수정·git checkout 모두 금지). 사용자에게 ① 바뀐 파일 ② 원인(어느 명령인지, "
                  "또는 세션 중 사용자가 Obsidian에서 고친 것인지) ③ 되돌리려면 사용자가 `git checkout HEAD -- <파일>`을 "
                  "실행하면 된다는 것을 보고하라. 같은 상태로는 다시 막지 않고, 커밋 제목에 표시가 붙는다.\n- "
                  + "\n- ".join(raw_changed[:30]), len(raw_changed))
    fp = wiki_fingerprint()
    if sess and sess["fp"] == fp:
        # 위키는 그대로 — 막지 않는다. .claude/ 설정 등 다른 변경이 있으면 이 세션 작업으로 커밋
        finish(commit_session_work(prefix), None)
    if not sess and state.get("clean_fp") == fp and not errors:
        finish(commit_session_work(prefix), None)  # (SessionStart 기록이 없는 세션) 마지막 clean 이후 바뀐 게 없음
    pre = set(sess["pre"]) if sess else set()
    new = [e for e in errors if e not in pre]

    # 1) 기계 검사: 이 세션이 만든 오류
    if new:
        block(state, sid,
              f"CLAUDE.md 규칙 위반 {len(new)}건. "
              "고친 뒤 `python3 .claude/wiki_lint.py`로 오류 0을 확인하고, 의미 검토(wiki-reviewer 서브에이전트)까지 마친 다음 끝내세요. "
              "raw 파일명 위반이나 사용자가 직접 고친 내용은 고치지 말고 사용자에게 보고만 하세요.\n- "
              + "\n- ".join(new[:30]), len(new))
    if not errors:
        mark_clean(state, log, reset_loops=False)  # 의미 검토 차단이 남았을 수 있어 상한 카운트는 유지

    # 2) 의미 검토: 이 세션이 바꾼 페이지가 있으면, 현재 내용에 대한 wiki-reviewer 영수증(문제 0건)이 필요
    if sess and "pages" in sess:
        now = page_hashes()
        changed = sorted(n for n in now if sess["pages"].get(n) != now[n])
        removed = sorted(n for n in sess["pages"] if n not in now)
        receipt = state.get("reviews", {}).get(content_fp(now))
        if (changed or removed) and receipt != 0:
            why = ("현재 내용으로 돈 의미 검토가 없다" if receipt is None
                   else f"마지막 의미 검토에서 문제 {receipt}건 — 고친 뒤 다시 검토받아야 한다")
            block(state, sid,
                  f"이 세션이 위키 페이지를 바꿨는데 {why}. "
                  f"Agent 도구로 `{REVIEWER}` 서브에이전트를 불러 아래 페이지와 관련 raw 파일을 넘기고, "
                  "보고된 문제를 고친 뒤 새 문제 0건이 나올 때까지 다시 검토받으세요 (`wiki-verify` 스킬 2번). "
                  "직접 영수증을 만들지 마세요.\n- 바뀐 페이지: "
                  + ", ".join(f"wiki/{n}" for n in changed)
                  + (("\n- 삭제된 페이지: " + ", ".join(removed)) if removed else ""),
                  len(changed) + len(removed))

    state.setdefault("loops", {}).pop(sid, None)  # 모두 통과: 이 세션의 차단 카운트 초기화
    save_state(state)
    finish(commit_session_work(prefix),
           f"위키: 세션 시작 전부터 있던 규칙 위반 {len(errors)}건 — 사용자 확인 필요 (`python3 .claude/wiki_lint.py`)" if errors else None)


def finish(committed, message):
    """검증 통과로 끝낼 때: 커밋 결과와 알림을 한 번에 출력한다."""
    msgs = [m for m in (message, None if committed else "위키: 검증은 통과했지만 git 커밋 실패 — `git status` 확인 필요") if m]
    if msgs:
        print(json.dumps({"systemMessage": " / ".join(msgs)}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
