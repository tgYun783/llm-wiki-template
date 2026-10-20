#!/usr/bin/env python3
"""위키 장치 회귀 스위트 러너. 설계: plan.md, 형식: README.md

  python3 .claude/regress/run.py validate   # registry·케이스 형식 검사 (오류 있으면 종료 코드 1)
  python3 .claude/regress/run.py mkvault <case> [--variant <이름>] [--run-id <id>] [--headless]
                                            # 미니 볼트를 만들고 경로를 출력 (손 실험용 최소판)
  python3 .claude/regress/run.py A [<case> ...] [--root DIR] [--lint FILE]
                                            # A층 케이스 실행 (PASS/FAIL 줄 + 요약, 종료 코드 0 | 1 | 2)
  python3 .claude/regress/run.py B --select [--files <경로> ...] [--since <REF>] [--root DIR]
                                            # 바뀐 파일로 돌릴 B층 케이스를 고름 (`<id> ← <이유>` 줄 + 요약, 종료 코드 0 | 2)
  python3 .claude/regress/run.py B --prepare <run-id> [<유닛> ...] [--files <경로> ... | --since <REF>] [--root DIR] [--base DIR]
                                            # 유닛마다 미니 볼트 v<k>/·요청문 v<k>.request.md를 <base>/<run-id>/에 만듦
                                            # (`v<k> ← <유닛>` 줄 + 요약, 종료 코드 0 | 2). 유닛이 없으면 --select와 같은 입력으로 고름
  python3 .claude/regress/run.py B --judge <run-id> <볼트> [<리뷰어 출력 파일> <transcript>] [--projects DIR] [--detect 예|아니오] [--not-detect 없음|있음] [--root DIR] [--base DIR]
                                            # 볼트 하나를 판정 (마지막 줄 `판정: <유닛> …`, 종료 코드 0 최종 PASS·FLAKY | 1 최종 FAIL·무효 반복
                                            # | 3 재실행·세션 대조 필요 | 2 판정 불가). 리뷰어는 부르지 않는다 — 부르는 것은 세션(README "B층 실행")
                                            # 출력 파일·transcript를 빼면 <projects>(기본 ~/.claude/projects/<root>)에서 첫 요청에
                                            # 그 볼트 경로가 있는 리뷰어 호출을 찾아 출력을 꺼낸다

표준 라이브러리만 쓴다(PyYAML 없음). registry·케이스는 README의 YAML 부분집합으로 쓴다.
"""
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REGRESS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(REGRESS))
LAYERS = ("A", "B")
CAUSES = ("절차", "규칙 공백")          # wiki-verify 2단계 원인 판정 ("정답이 갈림"은 케이스를 만들지 않음)
DEMOTE = ("예", "부분", "아니오")
# A층 runner: 검사기를 어떤 모드로 돌리나 → 허용 파일 칸, 기대 키
RUNNERS = {
    "lint": {"prefixes": ("wiki/", "raw/"), "expect": ("must_output", "must_not_output")},
    "hook-review-done": {"prefixes": ("wiki/", "raw/", "hook/"), "expect": ("receipt", "must_output", "must_not_output"),
                         "required": ("hook/input.json",)},
    # base/<이름> = 커밋된 wiki/<이름>. 러너가 임시 폴더를 git 저장소로 만들어 base를 커밋한 뒤 wiki/·raw/ 칸을 덮어쓴다
    "lint-git": {"prefixes": ("wiki/", "raw/", "base/"), "expect": ("must_output", "must_not_output")},
}
A_TODAY = "2026-01-15"   # lint-git: 검사기의 "오늘"(WIKI_LINT_TODAY) — 케이스가 날짜를 고정해 쓸 수 있게
B_PREFIXES = ("wiki/", "raw/")
META_REQUIRED = ("id", "층", "failure_class", "원인판정", "출처", "components", "private")
META_KEYS = META_REQUIRED + ("files", "runner", "내릴 수 있나", "precheck", "수정한 페이지")
B_EXPECT = ("reviewer_items", "reviewer_items_any", "must_detect", "must_not_detect")
FENCE = "~~~~"
HEADING = re.compile(r"^(##|###) (기대|메모|수정한 페이지|파일: (.+)|변형: (.+))$")


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


def scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1]
    if s == "null" or s == "":
        return None
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def value(s):
    s = s.strip()
    if s.startswith("["):
        if not s.endswith("]"):
            raise ValueError(f"닫는 ] 없음: {s}")
        inner = s[1:-1].strip()
        return [scalar(a or b) for a, b in re.findall(r'\s*("[^"]*")\s*(?:,|$)|\s*([^,"]+?)\s*(?:,|$)', inner)] if inner else []
    return scalar(s)


def parse_simple(text):
    """YAML 부분집합: `키: 값`, 인라인 목록 `[a, b]`, 키 아래 `- 항목` 목록, 한 단계 들여쓴 `키: 값` 맵.
    줄 전체 주석(#)만 허용. 지원하지 않는 모양은 ValueError."""
    data, cur = {}, None
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- ") or s == "-":
            if cur is None or not isinstance(data[cur], (list, type(None))):
                raise ValueError(f"{i}줄: 목록 항목 앞에 빈 키가 없음")
            data[cur] = (data[cur] or []) + [scalar(s[1:])]
            continue
        m = re.match(r"^([^\s:#][^:]*):(?:\s+(.*))?$", s)
        if not m:
            raise ValueError(f"{i}줄: 지원하지 않는 모양: {s}")
        k, v = m.group(1).strip(), m.group(2)
        if line[0] in " \t":
            if cur is None or not isinstance(data[cur], (dict, type(None))):
                raise ValueError(f"{i}줄: 들여쓴 맵 항목 앞에 빈 키가 없음")
            data[cur] = data[cur] or {}
            if k in data[cur]:
                raise ValueError(f"{i}줄: 중복 키 {k}")
            data[cur][k] = value(v or "")
            continue
        if k in data:
            raise ValueError(f"{i}줄: 중복 키 {k}")
        data[k] = value(v) if v else None
        cur = k if not v else None
    return data


def parse_case(text):
    """케이스 파일 → {meta, 기대, files, variants}. 형식 오류는 ValueError."""
    fm, body = split_fm(text)
    if fm is None:
        raise ValueError("frontmatter 없음")
    case = {"meta": parse_simple(fm), "기대": None, "files": {}, "variants": {}}
    target, sec, buf, fence = case, None, [], None   # target: 지금 채우는 케이스 또는 변형

    def close():
        if sec == "기대":
            if target["기대"] is not None:
                raise ValueError("기대 칸이 두 번 나옴")
            target["기대"] = parse_simple("\n".join(buf))
        elif sec == "수정한 페이지":     # 변형만 — 본 케이스는 frontmatter 칸
            if target is case:
                raise ValueError("수정한 페이지 칸은 변형 안에서만 (본 케이스는 frontmatter)")
            if target["수정한 페이지"] is not None:
                raise ValueError("수정한 페이지 칸이 두 번 나옴")
            lines = [x.strip() for x in buf if x.strip()]
            if any(not x.startswith("- ") for x in lines):
                raise ValueError("수정한 페이지 칸은 `- <경로>` 줄만")
            target["수정한 페이지"] = [x[2:].strip() for x in lines]
        elif sec is None and "".join(buf).strip():
            raise ValueError("첫 칸 제목 앞에 본문이 있음")

    for i, line in enumerate(body.splitlines(), 1):
        if fence is not None:
            if line == FENCE:
                if fence in target["files"]:
                    raise ValueError(f"파일 칸 중복: {fence}")
                target["files"][fence] = "\n".join(buf) + "\n"
                fence, buf = None, []
            else:
                buf.append(line)
            continue
        m = HEADING.match(line)
        if m:
            close()
            level, name, path, var = m.groups()
            if var is not None:
                if level != "##":
                    raise ValueError(f"본문 {i}줄: 변형은 ## 제목")
                if var in case["variants"]:
                    raise ValueError(f"변형 이름 중복: {var}")
                target = case["variants"][var] = {"기대": None, "files": {}, "수정한 페이지": None}
            elif level == "##" and target is not case:
                target = case       # 변형 뒤의 ## 기대·파일은 본 케이스로 돌아감
            elif level == "###" and target is case:
                raise ValueError(f"본문 {i}줄: ### 제목은 변형 안에서만")
            sec, buf = ("파일" if path else name), []
            if path:
                sec = "파일:" + path.strip()
            continue
        if line == FENCE:
            if not (sec or "").startswith("파일:") or "".join(buf).strip():
                raise ValueError(f"본문 {i}줄: {FENCE}는 파일 칸 제목 바로 아래에만")
            fence, buf = sec[3:], []
            continue
        if (sec or "").startswith("파일:") and line.strip():
            raise ValueError(f"본문 {i}줄: 파일 내용은 {FENCE} 울타리 안에")
        buf.append(line)
    if fence is not None:
        raise ValueError(f"파일 칸 울타리가 닫히지 않음: {fence}")
    close()
    return case


def load_registry(regress):
    reg = parse_simple(read(os.path.join(regress, "registry.yaml")))
    for k, t in (("components", dict), ("broad", list), ("failure_classes", dict)):
        if not isinstance(reg.get(k), t):
            raise ValueError(f"registry.yaml: {k}가 {'맵' if t is dict else '목록'}이 아님")
    return reg


def matches(root, pattern):
    return glob.glob(os.path.join(root, pattern), recursive=True)


def reviewer_item_count(root):
    path = os.path.join(root, ".claude", "agents", "wiki-reviewer.md")
    if not os.path.exists(path):
        return None
    m = re.search(r"^## 검토 항목\n(.*?)(?=^## )", read(path), re.M | re.S)
    return len(re.findall(r"^\d+\. ", m.group(1), re.M)) if m else None


def check_expect(E, cid, label, layer, runner, exp, n_items):
    if exp is None:
        return E(cid, f"{label}기대 칸 없음")
    allowed = RUNNERS[runner]["expect"] if layer == "A" else B_EXPECT
    who = f"runner {runner}" if layer == "A" else "층 B"
    for k in set(exp) - set(allowed):
        E(cid, f"{label}기대에 알 수 없는 키: {k} ({who} 허용: {', '.join(allowed)})")
    for k in allowed:
        if k != "receipt" and k in exp and not isinstance(exp[k], list):
            E(cid, f"{label}기대 {k}가 목록이 아님")
    lst = lambda k: exp.get(k) if isinstance(exp.get(k), list) else []
    if layer == "A":
        if runner == "hook-review-done":
            if "receipt" not in exp:
                E(cid, f"{label}기대에 receipt 없음 (기록될 issues 값, 영수증이 없어야 하면 null)")
            elif exp["receipt"] is not None and not isinstance(exp["receipt"], int):
                E(cid, f"{label}receipt는 정수 또는 null: {exp['receipt']}")
        elif not lst("must_output") and not lst("must_not_output"):
            E(cid, f"{label}기대에 must_output·must_not_output 둘 다 비었음")
        return
    if "reviewer_items" not in exp and "reviewer_items_any" not in exp:
        return E(cid, f"{label}기대에 reviewer_items 없음 (음성 케이스는 [], 하나 이상이면 되는 경우는 reviewer_items_any)")
    if "reviewer_items_any" in exp and not lst("reviewer_items_any"):
        E(cid, f"{label}reviewer_items_any가 비었음 (음성 케이스는 reviewer_items: [])")
    for k in ("reviewer_items", "reviewer_items_any"):
        for x in lst(k):
            if not isinstance(x, int) or x < 1 or (n_items and x > n_items):
                E(cid, f"{label}{k} 범위 밖: {x} (리뷰어 항목 1~{n_items or '?'})")
    positive = lst("reviewer_items") or lst("reviewer_items_any")
    if positive and not lst("must_detect"):
        E(cid, f"{label}양성 케이스(reviewer_items 있음)인데 must_detect 비었음")
    if not positive and not lst("must_not_detect"):
        E(cid, f"{label}음성 케이스(reviewer_items [])인데 must_not_detect 비었음")


def check_files(E, cid, label, layer, runner, files):
    prefixes = RUNNERS[runner]["prefixes"] if layer == "A" else B_PREFIXES
    for p in files:
        if not p.startswith(prefixes) or ".." in p.split("/") or p.count("/") != 1:
            E(cid, f"{label}파일 경로는 {' · '.join(x + '<이름>' for x in prefixes)}만 (하위 폴더 없음): {p}")
    if layer == "B" and not any(p.startswith("wiki/") for p in files):
        E(cid, f"{label}B층인데 wiki/ 파일 칸이 없음 (검토할 페이지)")
    for p in RUNNERS[runner].get("required", ()) if layer == "A" else ():
        if p not in files:
            E(cid, f"{label}runner {runner}에 필요한 파일 칸 없음: {p}")


def check_pages(E, cid, label, pages, files):
    """B층 수정한 페이지: 요청문에 넘길 페이지. 비교 대상 페이지와 가르려고 케이스가 직접 적는다."""
    if not isinstance(pages, list) or not pages:
        return E(cid, f"{label}B층인데 수정한 페이지 없음 (요청문에 넘길 wiki/ 페이지 목록)")
    for p in pages:
        if not (isinstance(p, str) and p.startswith("wiki/") and p in files):
            E(cid, f"{label}수정한 페이지는 이 케이스의 wiki/ 파일 칸이어야 함: {p}")


def validate(regress=REGRESS, root=ROOT):
    """registry·케이스 형식 검사. (errors, warns) — 각 항목은 '<대상>: <내용>'."""
    errors, warns = [], []
    E = lambda who, msg: errors.append(f"{who}: {msg}")
    W = lambda who, msg: warns.append(f"{who}: {msg}")
    try:
        reg = load_registry(regress)
    except (OSError, ValueError) as e:
        return [f"registry.yaml: {e}"], []
    comps, classes = reg["components"], reg["failure_classes"]
    for name, pats in comps.items():
        if not isinstance(pats, list) or not pats:
            E("registry.yaml", f"component {name}의 파일 목록이 비었거나 목록이 아님")
            continue
        for p in pats:
            if not matches(root, p):
                W("registry.yaml", f"component {name}: 맞는 파일 없음 {p}")
    for b in reg["broad"]:
        if b not in comps:
            E("registry.yaml", f"broad의 {b}가 components에 없음")

    cases = {}
    cdir = os.path.join(regress, "cases")
    for n in sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []:
        if not n.endswith(".md"):
            continue
        try:
            cases[n[:-3]] = parse_case(read(os.path.join(cdir, n)))
        except ValueError as e:
            E(n, str(e))
    n_items = reviewer_item_count(root)
    for cid, c in cases.items():
        meta = c["meta"]
        for k in META_REQUIRED:
            if k not in meta:
                E(cid, f"필수 칸 없음: {k}")
        for k in set(meta) - set(META_KEYS):
            E(cid, f"알 수 없는 칸: {k}")
        if "id" in meta and meta["id"] != cid:
            E(cid, f"id({meta['id']})가 파일 이름과 다름")
        layer = meta.get("층")
        if layer not in LAYERS:
            E(cid, f"층은 A | B: {layer}")
            continue
        if "failure_class" in meta and meta["failure_class"] not in classes:
            E(cid, f"failure_class가 registry 허용 목록에 없음: {meta['failure_class']}")
        if "원인판정" in meta and meta["원인판정"] not in CAUSES:
            E(cid, f"원인판정은 {' | '.join(CAUSES)}: {meta['원인판정']}")
        if "출처" in meta and (not isinstance(meta["출처"], list) or not meta["출처"]):
            E(cid, "출처가 비었음 (log 항목 제목 또는 커밋)")
        if "private" in meta and not isinstance(meta["private"], bool):
            E(cid, "private는 true | false")
        cs, fs = meta.get("components") or [], meta.get("files") or []
        if not isinstance(cs, list) or not isinstance(fs, list):
            E(cid, "components·files는 목록")
            cs, fs = [], []
        for x in cs:
            if x not in comps:
                E(cid, f"component가 registry에 없음: {x}")
        for f in fs:
            if not matches(root, f):
                E(cid, f"files의 경로가 없음: {f}")
        if not cs and not fs:
            E(cid, "components·files 둘 다 비었음 (선택기가 고를 수 없음)")
        runner = meta.get("runner")
        if layer == "A" and runner not in RUNNERS:
            E(cid, f"A층 runner는 {' | '.join(RUNNERS)}: {runner}")
            continue
        if layer == "B" and runner is not None:
            E(cid, "B층 케이스에는 runner를 두지 않음")
        demote, pre = meta.get("내릴 수 있나"), meta.get("precheck")
        if layer == "B":
            if demote not in DEMOTE:
                E(cid, f"내릴 수 있나는 {' | '.join(DEMOTE)}: {demote}")
            elif demote == "부분":
                if not pre:
                    E(cid, "내릴 수 있나: 부분인데 precheck 없음")
                elif pre not in cases:
                    E(cid, f"precheck 케이스가 없음: {pre}")
                elif cases[pre]["meta"].get("층") != "A":
                    E(cid, f"precheck는 A층 케이스여야 함: {pre}")
            elif demote == "예":
                W(cid, "내릴 수 있나: 예인데 B층에 남아 있음 → A층으로 옮기고 은퇴")
            if pre and demote != "부분":
                E(cid, "precheck는 내릴 수 있나: 부분일 때만")
        elif demote is not None or pre is not None:
            E(cid, "A층 케이스에는 내릴 수 있나·precheck를 두지 않음")
        if layer == "B":
            check_pages(E, cid, "", meta.get("수정한 페이지"), c["files"])
        elif "수정한 페이지" in meta:
            E(cid, "A층 케이스에는 수정한 페이지를 두지 않음")
        check_expect(E, cid, "", layer, runner, c["기대"], n_items)
        check_files(E, cid, "", layer, runner, c["files"])
        for vid, v in c["variants"].items():
            if v["기대"] is not None:
                check_expect(E, cid, f"변형 {vid}: ", layer, runner, v["기대"], n_items)
            check_files(E, cid, f"변형 {vid}: ", layer, runner, {**c["files"], **v["files"]})
            if layer == "B" and v["수정한 페이지"] is not None:
                check_pages(E, cid, f"변형 {vid}: ", v["수정한 페이지"], {**c["files"], **v["files"]})
            elif v["수정한 페이지"] is not None:
                E(cid, f"변형 {vid}: A층 케이스에는 수정한 페이지를 두지 않음")
            if not v["files"]:
                E(cid, f"변형 {vid}: 파일 칸 없음")
    return errors, warns


VAULT_BASE = "/tmp/wiki-regress"
# 미니 볼트에 넣는 시스템 파일 — 실행 시점의 현재 파일 (케이스에는 복사본을 두지 않음)
SYSTEM_FILES = ("CLAUDE.md", ".claude/agents/wiki-reviewer.md")
SYSTEM_DIRS = (".claude/rules",)


def mkvault(case_id, run_id=None, variant=None, headless=False, regress=REGRESS, root=ROOT, base=VAULT_BASE, name=None):
    """케이스 하나의 미니 볼트 <base>/<run-id>/<case>/를 만들고 경로를 돌려준다. name을 주면 폴더 이름은 그것(--prepare의 v<k>).
    headless면 SubagentStop hook만 둔 settings.json과 그 hook이 부를 wiki_lint.py 복사본을 넣는다
    (실제 settings.json을 복사하면 SessionStart·Stop 출력이 블라인드 입력을 흐림 — plan.md)."""
    case = parse_case(read(os.path.join(regress, "cases", case_id + ".md")))
    if case["meta"].get("층") != "B":
        raise ValueError(f"{case_id}: B층 케이스만 미니 볼트를 만든다")
    files = dict(case["files"])
    if variant is not None:
        if variant not in case["variants"]:
            raise ValueError(f"{case_id}: 변형 없음 {variant}")
        files.update(case["variants"][variant]["files"])
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
    vault = os.path.join(base, run_id, name or case_id + (f"--{variant}" if variant else ""))
    if os.path.exists(vault):
        raise ValueError(f"이미 있음: {vault} (실행마다 새로 — 다른 run-id를 쓴다)")
    for rel in SYSTEM_FILES:
        os.makedirs(os.path.dirname(os.path.join(vault, rel)), exist_ok=True)
        shutil.copy2(os.path.join(root, rel), os.path.join(vault, rel))
    for rel in SYSTEM_DIRS:
        shutil.copytree(os.path.join(root, rel), os.path.join(vault, rel))
    for d in ("wiki", "raw"):
        os.makedirs(os.path.join(vault, d), exist_ok=True)
    for rel, content in files.items():
        with open(os.path.join(vault, rel), "w", encoding="utf-8") as f:
            f.write(content)
    if headless:
        shutil.copy2(os.path.join(root, ".claude", "wiki_lint.py"), os.path.join(vault, ".claude", "wiki_lint.py"))
        hook = {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/wiki_lint.py" --review-done', "timeout": 30}
        with open(os.path.join(vault, ".claude", "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"hooks": {"SubagentStop": [{"hooks": [hook]}]}}, f, ensure_ascii=False, indent=2)
    return vault


A_USAGE = "사용법: run.py A [<case> ...] [--root DIR] [--lint FILE]"
A_ARGV = {"lint": [], "hook-review-done": ["--review-done"], "lint-git": []}   # runner → 검사기 인자


def load_a_cases(regress):
    """cases/*.md → [(id, case 또는 None, 형식 오류 문구 또는 None, 층)].
    형식 오류 케이스는 frontmatter에서 층을 읽어 보고, B로 읽히지 않으면 A층 실행에 넣어 FAIL로 센다."""
    out = []
    for path in sorted(glob.glob(os.path.join(regress, "cases", "*.md"))):
        cid, text = os.path.basename(path)[:-3], read(path)
        try:
            case = parse_case(text)
        except ValueError as e:
            try:
                layer = parse_simple(split_fm(text)[0] or "").get("층")
            except ValueError:
                layer = None
            out.append((cid, None, str(e), layer))
            continue
        out.append((cid, case, None, case["meta"].get("층")))
    return out


def compare_output(exp, lines):
    """stdout 줄을 must_output(어느 한 줄의 일부로 나와야 함)·must_not_output(어느 줄에든 나오면 안 됨)과 비교. 실패 이유(없으면 None)."""
    missing = [m for m in (exp.get("must_output") or []) if not any(str(m) in x for x in lines)]
    hit = [m for m in (exp.get("must_not_output") or []) if any(str(m) in x for x in lines)]
    reasons = [f"must_output 없음: {', '.join(map(str, missing))}" if missing else "",
               f"must_not_output 나옴: {', '.join(map(str, hit))}" if hit else ""]
    return "; ".join(r for r in reasons if r) or None


def abs_hook_paths(x, tmp):
    """JSON 값 안의 `hook/…` 문자열(중첩 dict·list 포함)을 임시 폴더 절대 경로로 바꾼다 (README)."""
    if isinstance(x, dict):
        return {k: abs_hook_paths(v, tmp) for k, v in x.items()}
    if isinstance(x, list):
        return [abs_hook_paths(v, tmp) for v in x]
    if isinstance(x, str) and x.startswith("hook/"):
        return os.path.join(tmp, x)
    return x


def read_receipt(tmp):
    """임시 폴더 .lint_state.json의 reviews 값 → (영수증 또는 None, 실패 이유 또는 None).
    파일 없음·읽기 실패·reviews 비었음은 None, 값이 여럿이면 이유."""
    try:
        with open(os.path.join(tmp, ".claude", ".lint_state.json"), encoding="utf-8") as f:
            reviews = json.load(f).get("reviews")
    except (OSError, ValueError, AttributeError):
        return None, None
    vals = list(reviews.values()) if isinstance(reviews, dict) else []
    if len(vals) > 1:
        return None, f"reviews 값이 여럿: {', '.join(map(str, vals))}"
    return (vals[0] if vals else None), None


def git_base(tmp, base):
    """lint-git: tmp를 git 저장소로 만들고 base(이름 → 내용)를 wiki/에 써서 커밋한다. 실패 이유(없으면 None).
    CLAUDE.md·검사기도 함께 커밋해 '커밋 안 된 변경'에는 케이스의 wiki/·raw/ 칸만 남게 한다."""
    for d in ("wiki", "raw"):  # 빈 raw/도 볼트의 정상 상태 — 검사기는 두 폴더가 있어야 돈다
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
    for name, content in base.items():
        with open(os.path.join(tmp, "wiki", name), "w", encoding="utf-8") as f:
            f.write(content)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=regress", "-c", "user.email=regress@example.invalid",
                  "commit", "-q", "--allow-empty", "-m", "base"]):
        p = subprocess.run(["git", "-C", tmp] + args, capture_output=True, text=True)
        if p.returncode != 0:
            return f"git {args[0] if args[0] != '-c' else 'commit'} 실패: {p.stderr.strip()}"
    return None


def run_a_one(runner, files, exp, root, lint):
    """임시 폴더에 파일 칸을 펼치고 검사기 복사본을 돌린다. 실패 이유(없으면 None)."""
    tmp = tempfile.mkdtemp(prefix="wiki-regress-a-")
    try:
        os.makedirs(os.path.join(tmp, ".claude"))
        shutil.copy2(os.path.join(root, "CLAUDE.md"), os.path.join(tmp, "CLAUDE.md"))
        shutil.copy2(lint, os.path.join(tmp, ".claude", "wiki_lint.py"))
        env = None
        if runner == "lint-git":
            why = git_base(tmp, {rel[len("base/"):]: c for rel, c in files.items() if rel.startswith("base/")})
            if why:
                return why
            files = {rel: c for rel, c in files.items() if not rel.startswith("base/")}
            env = dict(os.environ, WIKI_LINT_TODAY=A_TODAY)
        for rel, content in files.items():
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                f.write(content)
        stdin = ""
        if runner == "hook-review-done":
            try:
                stdin = json.dumps(abs_hook_paths(json.loads(files.get("hook/input.json", "")), tmp), ensure_ascii=False)
            except ValueError as e:
                return f"hook/input.json이 JSON이 아님: {e}"
        p = subprocess.run([sys.executable, os.path.join(tmp, ".claude", "wiki_lint.py")] + A_ARGV[runner],
                           cwd=tmp, input=stdin, capture_output=True, text=True, timeout=60, env=env)
        reasons = []
        if runner == "hook-review-done":
            got, why = read_receipt(tmp)
            if why:
                reasons.append(why)
            elif got != exp.get("receipt"):
                show = lambda v: "null" if v is None else str(v)
                reasons.append(f"receipt 기대 {show(exp.get('receipt'))}, 실제 {show(got)}")
        reasons.append(compare_output(exp, p.stdout.splitlines()))
        return "; ".join(r for r in reasons if r) or None
    except (OSError, subprocess.SubprocessError) as e:
        return f"실행 오류: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main_a(args, regress=REGRESS, root=ROOT):
    """run.py A — A층 케이스(변형 포함)마다 PASS/FAIL 한 줄, 마지막에 요약 줄. 종료 코드 0 | 1 | 2(사용법·케이스 지정 오류)."""
    ids, lint, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a in ("--root", "--lint"):
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                print(f"{a}에 값이 없음\n{A_USAGE}", file=sys.stderr)
                return 2
            if a == "--root":
                root = args[i + 1]
            else:
                lint = args[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            print(f"모르는 옵션: {a}\n{A_USAGE}", file=sys.stderr)
            return 2
        ids.append(a)
        i += 1
    lint = lint or os.path.join(root, ".claude", "wiki_lint.py")
    cases = load_a_cases(regress)
    by_id = {c[0]: c for c in cases}
    for cid in ids:
        if cid not in by_id:
            print(f"케이스 없음: {cid}", file=sys.stderr)
            return 2
        if by_id[cid][3] == "B":
            print(f"B층 케이스는 run.py A로 돌리지 않음: {cid}", file=sys.stderr)
            return 2
    chosen = [by_id[cid] for cid in dict.fromkeys(ids)] if ids else [c for c in cases if c[3] != "B"]
    t0, n_pass, n_fail = time.time(), 0, 0

    def report(rid, reason):
        nonlocal n_pass, n_fail
        if reason is None:
            n_pass += 1
            print(f"PASS {rid}")
        else:
            n_fail += 1
            print(f"FAIL {rid} — {reason}")

    for cid, case, err, _ in chosen:
        if err is not None:
            report(cid, err)
            continue
        runner = case["meta"].get("runner")
        if runner not in A_ARGV:
            report(cid, f"A층 runner는 {' | '.join(A_ARGV)}: {runner}")
            continue
        runs = [(cid, case["files"], case["기대"])]
        runs += [(f"{cid}--{vid}", {**case["files"], **v["files"]}, v["기대"] or case["기대"])
                 for vid, v in case["variants"].items()]
        for rid, files, exp in runs:
            report(rid, run_a_one(runner, files, exp or {}, root, lint))
    print(f"A층: 실행 {n_pass + n_fail} · PASS {n_pass} · FAIL {n_fail} · {time.time() - t0:.1f}s")
    return 1 if n_fail else 0


B_USAGE = ("사용법: run.py B --select [--files <경로> ...] [--since <REF>] [--root DIR]\n"
           "       run.py B --prepare <run-id> [<유닛> ...] [--files <경로> ... | --since <REF>] [--root DIR] [--base DIR]\n"
           "       run.py B --judge <run-id> <볼트> [<리뷰어 출력 파일> <transcript>] [--projects DIR] [--detect 예|아니오] "
           "[--not-detect 없음|있음] [--root DIR] [--base DIR]")
B_MODES = ("--select", "--prepare", "--judge")
B_MODE_OPTS = {"--select": ("--files", "--since", "--root"),
               "--prepare": ("--files", "--since", "--root", "--base"),
               "--judge": ("--detect", "--not-detect", "--root", "--base", "--projects")}
B_VALUES = {"--detect": ("예", "아니오"), "--not-detect": ("없음", "있음")}    # 허용 값 (세션 의미 대조 결과)


def path_glob(pattern, path):
    """registry glob을 파일 시스템이 아니라 경로 문자열에 맞춘다. `**`는 하위 폴더까지, `*`·`?`는 `/`를 넘지 않음."""
    rx = re.sub(r"\\\*\\\*|\\\*|\\\?", lambda m: {r"\*\*": ".*", r"\*": "[^/]*", r"\?": "[^/]"}[m.group()], re.escape(pattern))
    return re.fullmatch(rx, path) is not None


def git_changed(root, since=None):
    """git에서 바뀐 파일 목록. `diff -M --name-status -z <REF|HEAD>` + 추적 안 되는 파일. git 실패는 RuntimeError."""
    def git(*a):
        p = subprocess.run(["git", "-C", root] + list(a), capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"git {' '.join(a)} 실패: {p.stderr.strip() or p.returncode}")
        return [x for x in p.stdout.split("\0") if x]
    try:
        out, i = [], 0
        diff = git("diff", "-M", "--name-status", "-z", since or "HEAD")
        while i < len(diff):
            n = 3 if diff[i][:1] in ("R", "C") else 2     # rename·copy는 옛 경로·새 경로 둘
            out += diff[i + 1:i + n]
            i += n
        out += git("ls-files", "--others", "--exclude-standard", "-z")
    except OSError as e:
        raise RuntimeError(f"git 실행 실패: {e}")
    return list(dict.fromkeys(out))


def select_b(changed, regress=REGRESS, root=ROOT):
    """바뀐 파일 → 돌릴 B층 케이스 [(id, [이유, …]), …] (id 순). 출력과 분리 — 단계 6 --prepare가 다시 쓴다."""
    reg = load_registry(regress)
    comps, broad = reg["components"], reg["broad"]
    hit = {}                                    # component → 처음 맞은 바뀐 파일
    for name, pats in comps.items():
        for f in changed:
            if any(path_glob(p, f) for p in pats or []):
                hit.setdefault(name, f)
                break
    all_why = [f"broad {c} ({hit[c]})" for c in broad if c in hit]    # broad가 바뀌면 B층 전부
    cases_rel = os.path.relpath(os.path.join(regress, "cases"), root).replace(os.sep, "/")
    out = []
    for cid, case, _, layer in load_a_cases(regress):
        if layer != "B" or case is None:
            continue
        why = all_why + [f"{c} ({hit[c]})" for c in case["meta"].get("components") or []
                         if c in hit and c not in broad]
        why += [f"files ({f})" for f in case["meta"].get("files") or [] if f in changed]   # 경로 문자열 비교
        if f"{cases_rel}/{cid}.md" in changed:
            why.append("케이스 파일")
        if why:
            out.append((cid, why))
    return out


def b_units(changed, units, regress=REGRESS, root=ROOT):
    """--prepare 유닛 [{unit, case, variant, reasons}, …]. 유닛은 본 케이스 `<id>` 또는 변형 `<id>--<변형>`.
    units를 주면 그것만(준 순서, 이유 ["지정"]) — 하나라도 틀리면 아무것도 돌려주지 않고 ValueError.
    없으면 select_b(changed)가 고른 케이스마다 본 케이스 + 변형 전부(케이스 id 순, 변형은 파일 안 순서)."""
    cases = {cid: (case, layer) for cid, case, _, layer in load_a_cases(regress)}
    if units:
        out = []
        for u in units:
            cid, sep, var = u.partition("--")
            if cid not in cases:
                raise ValueError(f"없는 케이스: {u}")
            case, layer = cases[cid]
            if layer != "B" or case is None:
                raise ValueError(f"B층 케이스가 아님: {u}")
            if sep and var not in case["variants"]:
                raise ValueError(f"없는 변형: {u}")
            out.append({"unit": u, "case": cid, "variant": var if sep else None, "reasons": ["지정"]})
        return out
    out = []
    for cid, why in select_b(changed, regress, root):
        out.append({"unit": cid, "case": cid, "variant": None, "reasons": why})
        out += [{"unit": f"{cid}--{vid}", "case": cid, "variant": vid, "reasons": why}
                for vid in cases[cid][0]["variants"]]
    return out


def request_text(vault, pages):
    """요청문 — 세션이 그대로 wiki-reviewer에 넘긴다. 경로는 모두 볼트 절대 경로, 목록은 `, `로 잇는다.
    규칙은 spec 예시대로 CLAUDE.md 먼저, 그 뒤 .claude/rules/ 경로 순. 관련 raw는 볼트 raw/ 전부(변형 덮어쓰기 뒤).
    pages(수정한 페이지)만 넘기고 비교 대상 wiki/ 페이지는 적지 않는다."""
    V = os.path.abspath(vault)
    rules_dir = os.path.join(V, ".claude", "rules")
    rules = sorted(os.path.relpath(os.path.join(d, n), V).replace(os.sep, "/")
                   for d, _, names in os.walk(rules_dir) for n in names)
    raws = sorted("raw/" + n for n in os.listdir(os.path.join(V, "raw")))
    join = lambda rels: ", ".join(f"{V}/{r}" for r in rels)
    return ("자료 넣기 작업을 검토해 줘.\n\n"
            f"볼트 위치: {V}/ (검토 범위: 이 폴더. 경로는 모두 이 폴더 기준 절대 경로로 읽는다)\n"
            f"- 규칙: {join(['CLAUDE.md'] + rules)}\n"
            f"- 수정한 페이지: {join(sorted(pages))}\n"
            f"- 관련 raw: {join(raws)}\n")


BLIND_WORDS = ("기대", "failure", "must_", "test", "테스트", "회귀", "regress", "케이스")   # 요청문에 보이면 검사 중임이 드러나는 낱말


def blind_leaks(text, run_dir, cid, case):
    """요청문에서 실행 폴더 경로를 지운 나머지에 실패 유형이 드러나는 것 — 걸린 것 목록(없으면 []), 대소문자 무시.
    금지 낱말, 케이스 id, 그 케이스의 변형 이름 전부, failure_class 값, 본 케이스·변형 기대의 must_detect·must_not_detect 문장.
    고정 문구에는 이것들이 없으니 실제로 걸리는 것은 케이스가 정한 파일 이름(raw·수정한 페이지)이다."""
    rest = text.replace(run_dir.rstrip("/") + "/", "").lower()
    words = list(BLIND_WORDS) + [cid, *case["variants"], case["meta"].get("failure_class")]
    for exp in [case["기대"]] + [v["기대"] for v in case["variants"].values()]:
        for k in ("must_detect", "must_not_detect"):
            words += (exp or {}).get(k) or []
    return [w for w in dict.fromkeys(str(w) for w in words if w) if w.lower() in rest]


def unit_vault(run_dir, name, u, regress, root):
    """유닛 하나의 미니 볼트 <run_dir>/<name>/을 만들고 요청문 <name>.request.md를 쓴 뒤 볼트 경로를 돌려준다
    (--prepare와 --judge 재실행이 함께 쓴다 — 블라인드 보호가 두 곳에 복사되지 않게).
    블라인드 위반이면 요청문을 쓰지 않고 ValueError — 무엇을 지울지(실행 폴더 전체·이 볼트만)는 부르는 쪽이 정한다."""
    base, run_id = os.path.split(run_dir)
    vault = mkvault(u["case"], run_id, u["variant"], regress=regress, root=root, base=base, name=name)
    case = parse_case(read(os.path.join(regress, "cases", u["case"] + ".md")))
    var = case["variants"][u["variant"]] if u["variant"] is not None else {}
    pages = var.get("수정한 페이지") or case["meta"]["수정한 페이지"]   # 변형 칸이 없으면 본 케이스 frontmatter
    text = request_text(vault, pages)
    leaks = blind_leaks(text, run_dir, u["case"], case)
    if leaks:
        raise ValueError(f"블라인드 위반: {u['unit']} — {', '.join(leaks)}")
    with open(os.path.join(run_dir, f"{name}.request.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return vault


def prepare_b(run_id, picked, regress=REGRESS, root=ROOT, base=VAULT_BASE):
    """유닛마다 미니 볼트 <base>/<run-id>/v<k>/(k는 준비 순서, 세션 방식)와 요청문 v<k>.request.md(볼트 밖),
    manifest.json을 만들고 볼트 경로 목록을 돌려준다. 다 만든 뒤 유닛마다 <regress>/history.jsonl에 "선택됨" 한 줄을 더한다.
    폴더 이름에 케이스 id·변형 이름을 넣지 않는다 — 경로로 리뷰어에게 실패 유형이 보이지 않게. 유닛은 manifest로 되찾는다."""
    base = os.path.abspath(base)                # 요청문의 <V>는 볼트 절대 경로 — 상대 --base여도
    run_dir = os.path.join(base, run_id)
    if os.path.exists(run_dir):
        raise ValueError(f"이미 있음: {run_dir} (실행마다 새로 — 다른 run-id를 쓴다)")
    manifest, vaults = {}, []
    for k, u in enumerate(picked, 1):
        name = f"v{k}"
        try:
            vaults.append(unit_vault(run_dir, name, u, regress, root))
        except ValueError as e:
            if str(e).startswith("블라인드 위반"):
                shutil.rmtree(run_dir)          # 반쯤 만든 실행 폴더를 남기지 않는다 — 이름을 고친 뒤 같은 run-id로 다시 돌릴 수 있게
            raise
        manifest[name] = {"unit": u["unit"], "case": u["case"], "variant": u["variant"],
                          "round": 1, "attempt": 1, "result": None}
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    today = datetime.date.today().isoformat()
    with open(os.path.join(regress, "history.jsonl"), "a", encoding="utf-8") as f:   # 다 만든 뒤에만 — 실패한 준비는 남기지 않는다
        for k, u in enumerate(picked, 1):
            f.write(json.dumps({"event": "선택됨", "date": today, "run_id": run_id, "vault": f"v{k}",
                                "unit": u["unit"], "case": u["case"], "variant": u["variant"],
                                "reasons": u["reasons"]}, ensure_ascii=False) + "\n")
    return vaults


def problem_lines(text):
    """리뷰어 출력의 문제 줄 — `문제:` 절 안의 줄머리 `- [N]`(N은 정수) 줄. 절은 `문제:` 다음 줄부터 줄머리가 `- `도
    빈칸도 아닌 첫 줄(`참고:`·`REVIEW-RESULT` 등) 앞까지 — 참고 절의 `- [N]`을 세면 리뷰어가 문제로 세지 않은 것이
    맞은 것처럼 보인다(2026-10-19 b-full-2)."""
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith("문제:"):
            inside = True
            continue
        if inside and line.strip() and not line.startswith(("- ", " ", "\t")):
            inside = False
        if inside and re.match(r"- \[\d+\]", line):
            lines.append(line)
    return lines


def parse_review(text):
    """리뷰어 출력 → (문제 줄 항목 번호 집합, issues 정수 | None). 문제 줄은 problem_lines.
    REVIEW-RESULT는 hook(wiki_lint.py)과 같은 정규식으로 찾는다 — 두 쪽 판단이 갈리지 않게."""
    found = {int(re.match(r"- \[(\d+)\]", line).group(1)) for line in problem_lines(text)}
    m = re.search(r"REVIEW-RESULT: issues=(-?\d+)", text)
    return found, int(m.group(1)) if m else None


def b_expected(case, variant):
    """유닛의 기대 — 변형에 `### 기대`가 있으면 그것, 없으면 본 케이스 것(A층 러너의 `v["기대"] or case["기대"]`와 같은 우선)."""
    return (case["variants"][variant]["기대"] if variant else None) or case["기대"]


def judge_items(expected, text):
    """결정적 대조 → (결과, 사유 목록). REVIEW-RESULT 줄이 없으면 ("무효", ["출력 형식"]).
    기대 reviewer_items가 모두(all-of) 문제 줄 번호에 있고, reviewer_items_any가 있으면 그중 하나 이상(any-of)이 있으면 ("맞음", [])
    — 여분 번호는 FAIL 아님, 음성 []은 늘 맞음. 아니면 ("FAIL", [])."""
    found, issues = parse_review(text)
    if issues is None:
        return "무효", ["출력 형식"]
    any_of = set(expected.get("reviewer_items_any") or [])
    ok = set(expected.get("reviewer_items") or []) <= found and (not any_of or any_of & found)
    return ("맞음", []) if ok else ("FAIL", [])


def transcript_records(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def tool_paths(records, root):
    """assistant 레코드의 tool_use 중 Read file_path·Grep/Glob path(없으면 cwd)·Glob 절대 pattern(와일드카드 앞 폴더)을
    그 레코드 cwd(없으면 root) 기준 절대 경로로 푼다. api-request-blob 등 다른 레코드의 같은 tool_use는 보지 않는다."""
    out = []
    for rec in records:
        content = (rec.get("message") or {}).get("content") if rec.get("type") == "assistant" else None
        for c in content if isinstance(content, list) else []:
            if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                continue
            name, inp = c.get("name"), c.get("input") or {}
            ps = []
            if name == "Read":
                ps.append(inp.get("file_path") or "")
            elif name in ("Grep", "Glob"):
                ps.append(inp.get("path") or "")
                pat = inp.get("pattern") or ""
                if name == "Glob" and pat.startswith("/"):
                    head = re.split(r"[*?\[{]", pat, maxsplit=1)
                    ps.append(os.path.dirname(head[0]) if len(head) > 1 else pat)
            out += [os.path.normpath(os.path.join(rec.get("cwd") or root, p)) for p in ps]
    return out


def first_user_text(records):
    """첫 user 레코드의 글 (content가 목록이면 text 조각을 줄로 이음). 없거나 글이 아니면 None."""
    first = next((r for r in records if r.get("type") == "user"), None)
    content = ((first or {}).get("message") or {}).get("content")
    if isinstance(content, list):
        content = "\n".join(c.get("text") or "" for c in content if isinstance(c, dict) and c.get("type") == "text")
    return content if isinstance(content, str) else None


def projects_dir(root):
    """세션 기록 폴더 ~/.claude/projects/<root 절대 경로의 영숫자 밖 글자를 `-`로>."""
    return os.path.join(os.path.expanduser("~/.claude/projects"), re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(root)))


def find_call(vault, projects):
    """리뷰어 호출 transcript 경로 — <projects>/*/subagents/*.jsonl 중 첫 user 레코드에 `<vault>/`가 있는 것의 가장 최근 것.
    meta.json이 있으면 agentType wiki-reviewer만. 없으면 ValueError, 그 호출의 meta가 finished가 아니면 ValueError(실행 중)."""
    vault = os.path.normpath(os.path.abspath(vault))
    hits = []
    for p in glob.glob(os.path.join(projects, "*", "subagents", "*.jsonl")):
        meta = {}
        if os.path.isfile(p[:-len(".jsonl")] + ".meta.json"):
            with open(p[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("agentType") not in (None, "wiki-reviewer"):
                continue
        with open(p, encoding="utf-8") as f:     # 첫 user 레코드까지만 읽는다 — 기록이 수백 개라
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "user":
                    if vault + "/" in (first_user_text([rec]) or ""):
                        hits.append((os.path.getmtime(p), p, meta))
                    break
    if not hits:
        raise ValueError(f"리뷰어 호출 없음: 첫 요청에 {vault}/ 가 있는 transcript가 {projects}에 없음")
    _, path, meta = max(hits)
    if meta and not meta.get("finished"):
        raise ValueError(f"리뷰어 호출이 아직 실행 중: {path}")
    return path


def review_output(transcript):
    """transcript → 리뷰어 최종 출력. assistant 레코드의 SubagentHandback tool_use `message`나 REVIEW-RESULT가 든 text 중
    마지막 것(세션 방식 리뷰어의 최종 출력은 SubagentHandback으로 남는다 — 2026-10-19 6c). 없으면 ""."""
    text = ""
    for rec in transcript_records(transcript):
        content = (rec.get("message") or {}).get("content") if rec.get("type") == "assistant" else None
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "SubagentHandback":
                text = (c.get("input") or {}).get("message") or ""
            elif c.get("type") == "text" and "REVIEW-RESULT:" in (c.get("text") or ""):
                text = c["text"]
    return text


def transcript_leaks(text, vault, root):
    """누출 사유 목록(없으면 []) — `--root`의 wiki/·raw/ 안이나 `--root` 자체면 "실제 볼트 <경로>",
    실행 폴더 안이지만 자기 볼트 밖(다른 v<k>·manifest·요청문)이면 "실행 폴더 <경로>".
    첫 user 레코드에 이 볼트 경로가 없으면 다른 실행의 기록이라 ValueError. 두 쪽 모두 abspath·normpath만 쓴다(realpath 아님 —
    요청문의 <V>가 abspath라서, 한쪽만 풀면 macOS의 /tmp → /private/tmp로 어긋난다)."""
    vault, root = os.path.normpath(os.path.abspath(vault)), os.path.normpath(os.path.abspath(root))
    run_dir = os.path.dirname(vault)
    records = transcript_records(text)
    content = first_user_text(records)
    if content is None or vault + "/" not in content:
        raise ValueError(f"다른 실행의 transcript: 첫 user 레코드에 {vault}/ 없음")
    inside = lambda p, d: p == d or p.startswith(d + os.sep)   # 경로 경계로 — v1이 v10을, wiki가 wiki-old를 잡지 않게
    leaks = []
    for p in tool_paths(records, root):
        if p == root or inside(p, os.path.join(root, "wiki")) or inside(p, os.path.join(root, "raw")):
            leaks.append(f"실제 볼트 {p}")
        elif inside(p, run_dir) and not inside(p, vault):
            leaks.append(f"실행 폴더 {p}")
    return list(dict.fromkeys(leaks))


SEMANTIC_OK = (("must_detect", "예"), ("must_not_detect", "없음"))   # 기대 칸 → PASS가 되는 인자 값


def judge_semantic(expected, detect, not_detect):
    """의미 대조 인자 → "PASS" | "FAIL" | None(세션 대조 필요). 빈 기대 칸(키 없음·[])에 해당하는 인자는 무시한다.
    필요한 인자가 하나라도 없으면 None. 무효·항목 FAIL이 먼저라는 순서는 부르는 쪽(--judge)이 지킨다."""
    need = [(v, ok) for (k, ok), v in zip(SEMANTIC_OK, (detect, not_detect)) if expected.get(k)]
    if any(v is None for v, _ in need):
        return None
    return "PASS" if all(v == ok for v, ok in need) else "FAIL"


def semantic_request(unit, expected, text):
    """세션 대조 필요일 때 출력할 줄 — 비지 않은 기대 문장(`must_detect: …`), 리뷰어 문제 줄(problem_lines — parse_review와 같은 기준),
    마지막 줄 `판정: <유닛> 세션 대조 필요`."""
    lines = [f"{k}: {s}" for k, _ in SEMANTIC_OK for s in expected.get(k) or []]
    problems = problem_lines(text)
    lines += ["리뷰어 문제 줄:" if problems else "리뷰어 문제 줄: 없음"] + problems
    return lines + [f"판정: {unit} 세션 대조 필요"]


INVALID_MAX = 3     # 같은 유닛·회차에서 무효가 이 수째면 새 볼트 없이 중단


def judge_b(run_id, name, review, transcript, detect=None, not_detect=None, regress=REGRESS, root=ROOT, base=VAULT_BASE):
    """볼트 하나의 판정 → (종료 코드, 출력 줄 목록). 순서: 누출(transcript_leaks) → 항목(judge_items) → 의미(judge_semantic).
    누출·출력 형식이면 무효, 항목이 틀리면 의미 대조 없이 FAIL. 의미 인자가 모자라면 semantic_request 줄 + 3, 아무것도 쓰지 않는다.
    결과가 정해지면 회차 규칙(plan.md "B층 결과")으로 최종 결과·재실행을 정하고, 재실행 볼트를 다 만든 뒤에
    manifest(그 볼트 result, 새 볼트 항목)와 <regress>/history.jsonl("판정" 한 줄)에 쓴다.
    실행 폴더·manifest 없음, manifest에 없는 볼트, 이미 결과가 있는 볼트, 다른 실행의 transcript, 블라인드 위반은
    아무것도 쓰지 않고 ValueError(→ 2)."""
    base = os.path.abspath(base)
    run_dir = os.path.join(base, run_id)
    mpath = os.path.join(run_dir, "manifest.json")
    if not os.path.isdir(run_dir):
        raise ValueError(f"실행 폴더 없음: {run_dir}")
    if not os.path.isfile(mpath):
        raise ValueError(f"manifest 없음: {mpath}")
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    if name not in manifest:
        raise ValueError(f"manifest에 없는 볼트: {name}")
    ent = manifest[name]
    if ent["result"] is not None:
        raise ValueError(f"이미 결과가 있는 볼트: {name} ({ent['result']})")
    unit, rnd = ent["unit"], ent["round"]
    expected = b_expected(parse_case(read(os.path.join(regress, "cases", ent["case"] + ".md"))), ent["variant"])
    leaks = transcript_leaks(transcript, os.path.join(run_dir, name), root)   # 다른 실행이면 ValueError
    items, why = judge_items(expected, review)
    leaks += why
    if leaks:
        result = "무효"
    elif items == "FAIL":
        result = "FAIL"
    else:
        result = judge_semantic(expected, detect, not_detect)
        if result is None:
            return 3, semantic_request(unit, expected, review)
    lines = [f"무효 사유: {x}" for x in leaks]
    final, rerun = None, None
    if result == "무효":
        n = 1 + sum(1 for v in manifest.values() if v["unit"] == unit and v["round"] == rnd and v["result"] == "무효")
        if n >= INVALID_MAX:
            code, last = 1, f"판정: {unit} 무효 반복 — 중단"
        else:
            code, last, rerun = 3, f"판정: {unit} 무효 — 재실행", (rnd, ent["attempt"] + 1)
    elif rnd == 1 and result == "FAIL":
        code, last, rerun = 3, f"판정: {unit} 1회차 FAIL — 재실행", (2, 1)
    else:
        final = result if rnd == 1 else "FLAKY" if result == "PASS" else "FAIL"
        code, last = (0 if final in ("PASS", "FLAKY") else 1), f"판정: {unit} {rnd}회차 {result} — 최종 {final}"
    if rerun:
        new = f"v{len(manifest) + 1}"
        u = {k: ent[k] for k in ("unit", "case", "variant")}
        try:
            unit_vault(run_dir, new, u, regress, root)
        except ValueError:
            if os.path.exists(os.path.join(run_dir, new)):
                shutil.rmtree(os.path.join(run_dir, new))    # 기록 전이니 만든 볼트만 지우면 판정 전 상태 그대로
            raise
        manifest[new] = {**u, "round": rerun[0], "attempt": rerun[1], "result": None}
        lines.append(f"재실행 요청문: {os.path.join(run_dir, new + '.request.md')}")
    ent["result"] = result
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    row = {"event": "판정", "date": datetime.date.today().isoformat(), "run_id": run_id, "vault": name,
           "unit": unit, "case": ent["case"], "variant": ent["variant"], "round": rnd, "attempt": ent["attempt"],
           "result": result, "final": final, "items_expected": list(expected.get("reviewer_items") or []),
           "items_expected_any": list(expected.get("reviewer_items_any") or []),
           "items_found": sorted(parse_review(review)[0]), "leaks": leaks, "detect": detect, "not_detect": not_detect}
    with open(os.path.join(regress, "history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return code, lines + [last]


def main_b(args, regress=REGRESS, root=ROOT, base=VAULT_BASE):
    """run.py B --select | --prepare | --judge — 사용법은 B_USAGE. 사용법 오류는 일을 시작하기 전에 stderr 이유 + 종료 코드 2.
    --select: 고른 케이스마다 `<id> ← <이유>; …`, 마지막에 요약 줄. 종료 코드 0 | 2(사용법·validate·git 오류).
    --prepare: 유닛마다 `v<k> ← <유닛>`, 마지막에 요약 줄. 종료 코드 0 | 2(사용법·validate·유닛·실행 폴더가 이미 있음·블라인드 위반).
    --judge: 마지막 줄 `판정: <유닛> …`. 종료 코드 0(최종 PASS·FLAKY) | 1(최종 FAIL·무효 반복) | 3(재실행·세션 대조 필요) | 2(판정 불가).
      위치 인자가 2개면 find_call·review_output으로 transcript·출력을 찾고 첫 줄에 `transcript: <경로>` — 못 찾거나 출력에
      REVIEW-RESULT가 없으면 아무것도 쓰지 않고 2."""
    def usage(why):
        print(f"{why}\n{B_USAGE}", file=sys.stderr)
        return 2

    modes, pos, opts, files, i = [], {}, {}, None, 0
    while i < len(args):
        a = args[i]
        if a in B_MODES:
            modes.append(a)
            pos[a], i = [], i + 1
            if a != "--select":                 # 모드 바로 뒤 위치 인자: --prepare <run-id> [<유닛> ...], --judge 4개
                while i < len(args) and not args[i].startswith("--"):
                    pos[a].append(args[i])
                    i += 1
        elif a in ("--since", "--root", "--base", "--detect", "--not-detect", "--projects"):
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                return usage(f"{a}에 값이 없음")
            opts[a], i = args[i + 1], i + 2
        elif a == "--files":
            files, i = [], i + 1
            while i < len(args) and not args[i].startswith("--"):
                files.append(args[i])
                i += 1
        else:
            return usage(f"모르는 옵션: {a}")
    if not modes:
        return usage("모드가 없음: --select·--prepare·--judge 중 하나")
    if len(modes) > 1:
        return usage(f"모드는 하나만: {' '.join(modes)}")
    mode = modes[0]
    for o in list(opts) + (["--files"] if files is not None else []):
        if o not in B_MODE_OPTS[mode]:
            return usage(f"모르는 옵션: {o} ({mode}에서)")
    for o, ok in B_VALUES.items():
        if o in opts and opts[o] not in ok:
            return usage(f"{o} 값은 {'|'.join(ok)}만 허용: {opts[o]}")
    if files is not None and "--since" in opts:
        return usage("--files와 --since는 함께 쓰지 않음")
    if mode == "--prepare" and not pos[mode]:
        return usage("--prepare에 run-id가 없음")
    if mode == "--judge" and len(pos[mode]) not in (2, 4):
        return usage(f"--judge 위치 인자는 2개(<run-id> <볼트>) 또는 4개(+ <리뷰어 출력 파일> <transcript>) — {len(pos[mode])}개")
    if mode == "--judge" and len(pos[mode]) == 4 and "--projects" in opts:
        return usage("--projects는 위치 인자 2개(transcript 자동 찾기)일 때만")
    since, root, base = opts.get("--since"), opts.get("--root", root), opts.get("--base", base)
    if mode == "--judge":
        run_id, name = pos[mode][:2]
        found = []
        try:
            if len(pos[mode]) == 4:
                review, transcript = read(pos[mode][2]), read(pos[mode][3])
            else:                               # 자동: 첫 요청에 이 볼트 경로가 있는 리뷰어 호출을 찾아 출력을 꺼낸다
                tr_path = find_call(os.path.join(os.path.abspath(base), run_id, name),
                                    opts.get("--projects") or projects_dir(root))
                transcript = read(tr_path)
                review = review_output(transcript)
                if "REVIEW-RESULT:" not in review:   # 빈 출력을 넘기면 "무효(출력 형식)"로 기록되고 재실행 볼트가 생긴다
                    raise ValueError(f"리뷰어 출력에 REVIEW-RESULT 없음 — 판정하지 않음: {tr_path}")
                found = [f"transcript: {tr_path}"]
            code, lines = judge_b(run_id, name, review, transcript, opts.get("--detect"),
                                  opts.get("--not-detect"), regress, root, base)
        except (ValueError, OSError) as e:
            print(f"B층 판정 실패: {e}", file=sys.stderr)
            return 2
        print("\n".join(found + lines))
        return code
    label = "선택" if mode == "--select" else "준비"
    errors, _ = validate(regress, root)         # 형식 오류 케이스가 선택에서 조용히 빠지지 않게 — 경고로는 막지 않는다
    if errors:
        print(f"B층 {label} 거부: validate 오류\n" + "\n".join(f"오류 {e}" for e in errors), file=sys.stderr)
        return 2
    units = pos.get("--prepare", [])[1:]        # --prepare <run-id> 뒤의 유닛 — 주면 git·--files를 보지 않는다
    try:
        changed = None if units else files if files is not None else git_changed(root, since)
        picked = b_units(changed, units, regress, root) if mode == "--prepare" else select_b(changed, regress, root)
        total = sum(1 for c in load_a_cases(regress) if c[3] == "B" and c[1] is not None)
    except (RuntimeError, OSError, ValueError) as e:
        print(f"B층 {label} 실패: {e}", file=sys.stderr)
        return 2
    if mode == "--prepare":
        if not picked:
            print(f"B층 준비: 유닛 0 · 케이스 0/{total} · 바뀐 파일 {len(changed)}")
            return 0
        try:
            vaults = prepare_b(pos[mode][0], picked, regress, root, base)
        except (ValueError, OSError) as e:
            print(f"B층 준비 실패: {e}", file=sys.stderr)
            return 2
        for u, v in zip(picked, vaults):
            print(f"{os.path.basename(v)} ← {u['unit']}")
        ncase = len(dict.fromkeys(u["case"] for u in picked))
        print(f"B층 준비: 유닛 {len(picked)} · 케이스 {ncase}/{total} · 실행 폴더 {os.path.dirname(vaults[0])}")
        return 0
    for cid, why in picked:
        print(f"{cid} ← {'; '.join(why)}")
    print(f"B층 선택: {len(picked)}/{total} · 바뀐 파일 {len(changed)}")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "mkvault" and len(sys.argv) > 2:
        args = sys.argv[3:]
        opt = lambda k: args[args.index(k) + 1] if k in args and args.index(k) + 1 < len(args) else None
        try:
            print(mkvault(sys.argv[2], run_id=opt("--run-id"), variant=opt("--variant"), headless="--headless" in args))
        except (ValueError, OSError) as e:
            sys.exit(f"mkvault: {e}")
        sys.exit(0)
    if cmd == "validate":
        errors, warns = validate()
        for e in errors:
            print("오류", e)
        for w in warns:
            print("경고", w)
        n = len([n for n in os.listdir(os.path.join(REGRESS, "cases")) if n.endswith(".md")]) \
            if os.path.isdir(os.path.join(REGRESS, "cases")) else 0
        print(f"validate: 케이스 {n}개, 오류 {len(errors)}, 경고 {len(warns)}")
        sys.exit(1 if errors else 0)
    if cmd == "A":
        sys.exit(main_a(sys.argv[2:]))
    if cmd == "B":
        sys.exit(main_b(sys.argv[2:]))
    print(__doc__.strip())
    sys.exit(2)


if __name__ == "__main__":
    main()
