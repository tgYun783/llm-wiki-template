#!/usr/bin/env python3
"""raw 등록 작업의 시간·토큰을 세션 기록에서 모아 ingest_history.jsonl에 한 줄 남긴다.

  start <raw 파일…>          표지(METRIC-START {...})를 출력한다 — 세션 기록에 남아 구간의 시작이 된다
  finish <nonce> [--b-run ID …] [--dry-run]
                              표지가 있는 세션 기록에서 그때부터 지금까지를 합산해 한 줄 append

상태 파일을 두지 않는다. 형식·필드 뜻은 같은 폴더의 README.md.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # 볼트 루트 (.claude/metrics/ 의 두 단계 위)
HISTORY = os.path.join(HERE, "ingest_history.jsonl")
REGRESS_HISTORY = os.path.join(ROOT, ".claude", "regress", "history.jsonl")
MARK = "METRIC-START "
REVIEWER = "wiki-reviewer"


def projects_dir(root):
    """Claude Code 세션 기록 폴더 (볼트 경로의 영숫자 밖 글자를 '-'로)."""
    return os.path.join(os.path.expanduser("~/.claude/projects"), re.sub(r"[^A-Za-z0-9]", "-", root))


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def now_local():
    return datetime.now(timezone.utc).astimezone()


def git_head(root):
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:
        return None


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def texts(rec):
    """레코드 안의 글(사용자 글·도구 결과)을 모두 꺼낸다."""
    c = rec.get("message", {}).get("content") if isinstance(rec.get("message"), dict) else None
    if isinstance(c, str):
        yield c
    elif isinstance(c, list):
        for x in c:
            if not isinstance(x, dict):
                continue
            if isinstance(x.get("text"), str):
                yield x["text"]
            cc = x.get("content")
            if isinstance(cc, str):
                yield cc
            elif isinstance(cc, list):
                for y in cc:
                    if isinstance(y, dict) and isinstance(y.get("text"), str):
                        yield y["text"]


def find_marker(recs, nonce):
    for r in recs:
        if r.get("type") != "user":
            continue
        for t in texts(r):
            for m in re.finditer(re.escape(MARK) + r"(\{[^\n]*\})", t):
                try:
                    d = json.loads(m.group(1))
                except ValueError:
                    continue
                if d.get("nonce") == nonce:
                    return d
    return None


def usage_by_id(recs):
    """assistant 응답은 블록마다 레코드가 나뉜다 — message.id로 하나만 (마지막 것)."""
    seen = {}
    for r in recs:
        m = r.get("message")
        if r.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict):
            seen[m.get("id") or r.get("uuid")] = m
    return seen


def sum_usage(msgs):
    keys = {"input": "input_tokens", "cache_creation": "cache_creation_input_tokens",
            "cache_read": "cache_read_input_tokens", "output": "output_tokens"}
    tot = {k: 0 for k in keys}
    for m in msgs.values():
        for k, u in keys.items():
            tot[k] += m["usage"].get(u) or 0
    return tot


def is_human(r):
    o = r.get("origin")
    return r.get("type") == "user" and isinstance(o, dict) and o.get("kind") == "human"


def is_stop_block(r):
    return r.get("type") == "user" and any(t.startswith("Stop hook feedback") for t in texts(r))


WORK_TYPES = {"assistant", "user", "system"}  # 대기 간격의 기준. attachment 등은 사람 메시지와 같은 시각에 붙어 기준이 못 된다


def wait_sec(recs):
    """사용자를 기다린 시간: 사람 메시지 직전 작업 기록부터의 간격 + AskUserQuestion 호출부터 답까지."""
    timed = [r for r in recs if r.get("timestamp")]
    timed.sort(key=lambda r: r["timestamp"])
    total = 0.0
    asks = {}
    last_work = None
    for r in timed:
        t = ts(r["timestamp"])
        if is_human(r):
            if last_work is not None:
                total += max(0.0, (t - last_work).total_seconds())
            last_work = t
            continue
        if r.get("type") in WORK_TYPES:
            last_work = t
        m = r.get("message")
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        for x in m["content"]:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "tool_use" and x.get("name") == "AskUserQuestion":
                asks[x.get("id")] = t
            elif x.get("type") == "tool_result" and x.get("tool_use_id") in asks:
                total += max(0.0, (t - asks.pop(x["tool_use_id"])).total_seconds())
    return total


def b_result(run_ids, path=REGRESS_HISTORY):
    """회귀 history에서 그 run들의 유닛별 마지막 최종 판정을 센다."""
    res = {"pass": 0, "flaky": 0, "fail": 0, "invalid": 0}
    if not run_ids or not os.path.exists(path):
        return 0, res
    last = {}
    for r in read_jsonl(path):
        if r.get("event") == "판정" and r.get("run_id") in run_ids and r.get("final"):
            last[(r["run_id"], r.get("unit"))] = r["final"]
    names = {"PASS": "pass", "FLAKY": "flaky", "FAIL": "fail"}
    for v in last.values():
        res[names.get(v, "invalid")] += 1
    return len(last), res


def cmd_start(args):
    raws, size = [], 0
    for p in args.raw:
        path = p if os.path.isabs(p) else os.path.join(ROOT, p if p.startswith("raw/") else os.path.join("raw", p))
        if not os.path.isfile(path):
            print(f"raw 파일 없음: {p}", file=sys.stderr)
            return 2
        with open(path, encoding="utf-8", errors="replace") as f:
            size += len(f.read())
        raws.append(os.path.basename(path))
    d = {"nonce": uuid.uuid4().hex[:12], "start": now_local().isoformat(timespec="seconds"),
         "raw": raws, "raw_size": size, "git_head": git_head(ROOT)}
    print(MARK + json.dumps(d, ensure_ascii=False))
    print(f"끝낼 때: python3 .claude/metrics/ingest_metrics.py finish {d['nonce']}")
    return 0


def collect(nonce, projects, b_runs, end=None):
    end = end or now_local()
    for main in sorted(glob.glob(os.path.join(projects, "*.jsonl")), key=os.path.getmtime, reverse=True):
        with open(main, encoding="utf-8", errors="replace") as f:
            if nonce not in f.read():
                continue
        recs = read_jsonl(main)
        mark = find_marker(recs, nonce)
        if mark:
            break
    else:
        return None
    start = ts(mark["start"])
    win = [r for r in recs if r.get("timestamp") and ts(r["timestamp"]) >= start and ts(r["timestamp"]) <= end]
    main_msgs = usage_by_id(win)
    all_msgs = dict(main_msgs)
    review = {}
    models = {"main": sorted({m.get("model") for m in main_msgs.values() if m.get("model")})}
    sub_dir = os.path.join(main[:-len(".jsonl")], "subagents")
    review_models = set()
    review_calls = 0
    for sp in sorted(glob.glob(os.path.join(sub_dir, "agent-*.jsonl"))):
        srecs = [r for r in read_jsonl(sp) if r.get("timestamp")]
        if not srecs or ts(min(r["timestamp"] for r in srecs)) < start:
            continue
        msgs = usage_by_id(srecs)
        all_msgs.update(msgs)
        meta_path = sp[:-len(".jsonl")] + ".meta.json"
        meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}
        if meta.get("agentType") == REVIEWER:
            review_calls += 1
            review_models |= {m.get("model") for m in msgs.values() if m.get("model")}
            u = sum_usage(msgs)
            stamps = sorted(r["timestamp"] for r in srecs)
            review["duration_sec"] = review.get("duration_sec", 0) + round((ts(stamps[-1]) - ts(stamps[0])).total_seconds())
            review["input_tokens"] = review.get("input_tokens", 0) + u["input"] + u["cache_creation"] + u["cache_read"]
            review["output_tokens"] = review.get("output_tokens", 0) + u["output"]
    if review_models:
        models["reviewer"] = sorted(review_models)
    models = {k: (v[0] if len(v) == 1 else v) for k, v in models.items() if v}
    u = sum_usage(all_msgs)
    duration = (end - start).total_seconds()
    stops = sum(1 for r in win if is_stop_block(r))
    b_sel, b_res = b_result(b_runs)
    versions = [r.get("version") for r in win if r.get("version")]
    row = {
        "metric_schema_version": SCHEMA_VERSION,
        "timestamp": end.isoformat(timespec="seconds"),
        "raw_id": mark["raw"], "raw_count": len(mark["raw"]), "raw_size": mark["raw_size"],
        "duration_sec": round(duration), "active_sec": round(max(0.0, duration - wait_sec(win))),
        "input_tokens": u["input"] + u["cache_creation"] + u["cache_read"],
        "cache_creation_tokens": u["cache_creation"], "cache_read_tokens": u["cache_read"],
        "output_tokens": u["output"],
        "llm_calls": len(all_msgs), "review_calls": review_calls,
        "retry_count": max(review_calls - 1, 0) + stops,
        "B_selected": b_sel, "B_result": b_res,
        "git_head": mark.get("git_head"), "model": models, "cc_version": versions[-1] if versions else None,
    }
    if review:
        row["phases"] = {"review": review}
    return row


def cmd_finish(args):
    row = collect(args.nonce, args.projects or projects_dir(ROOT), args.b_run)
    if row is None:
        print(f"표지 없음: {args.nonce} — start 출력이 남은 세션 기록을 찾지 못함", file=sys.stderr)
        return 2
    line = json.dumps(row, ensure_ascii=False)
    if not args.dry_run:
        with open(args.history or HISTORY, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("raw", nargs="+")
    s.set_defaults(func=cmd_start)
    f = sub.add_parser("finish")
    f.add_argument("nonce")
    f.add_argument("--b-run", nargs="*", default=[], help="이 작업에서 돌린 B층 run-id")
    f.add_argument("--dry-run", action="store_true", help="append하지 않고 출력만")
    f.add_argument("--projects", help="세션 기록 폴더 (기본: ~/.claude/projects/<볼트 경로>)")
    f.add_argument("--history", help="기록 파일 (기본: 이 폴더의 ingest_history.jsonl)")
    f.set_defaults(func=cmd_finish)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
