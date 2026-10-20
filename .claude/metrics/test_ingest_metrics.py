#!/usr/bin/env python3
"""ingest_metrics.py 테스트 — 가짜 세션 기록으로 합산을 확인한다. 실행: python3 .claude/metrics/test_ingest_metrics.py"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_metrics as im  # noqa: E402

NONCE = "abc123def456"
MARK = {"nonce": NONCE, "start": "2026-10-19T10:00:00+00:00", "raw": ["2026-10-18_a.md"], "raw_size": 1200, "git_head": "deadbee"}


def T(sec):
    return f"2026-10-19T10:{sec // 60:02d}:{sec % 60:02d}.000Z"


def asst(sec, mid, out=10, cr=100, cc=5, inp=1, model="claude-opus-5-5", content=None, version="2.1.302"):
    return {"type": "assistant", "timestamp": T(sec), "version": version,
            "message": {"id": mid, "model": model, "content": content or [{"type": "text", "text": "x"}],
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cc,
                                  "cache_read_input_tokens": cr, "output_tokens": out}}}


def tool_result(sec, text, tool_use_id="t0"):
    return {"type": "user", "timestamp": T(sec),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]}}


def human(sec, text="응"):
    return {"type": "user", "timestamp": T(sec), "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def stop_block(sec):
    return {"type": "user", "timestamp": T(sec), "isMeta": True, "message": {"role": "user", "content": "Stop hook feedback:\n[위키 검증 루프 1/5]"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def write(self, name, recs):
        p = os.path.join(self.d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    def session(self, recs, sid="s1"):
        return self.write(f"{sid}.jsonl", recs)

    def sub(self, sid, aid, recs, agent_type="wiki-reviewer"):
        self.write(f"{sid}/subagents/agent-{aid}.jsonl", recs)
        with open(os.path.join(self.d, sid, "subagents", f"agent-{aid}.meta.json"), "w") as f:
            json.dump({"agentType": agent_type}, f)

    def marker_rec(self, sec=0):
        return tool_result(sec, im.MARK + json.dumps(MARK))

    def collect(self, end_sec=600):
        return im.collect(NONCE, self.d, [], end=im.ts(T(end_sec)))


class TestCollect(Base):
    def test_dedupe_by_message_id(self):
        # 한 응답이 블록마다 두 레코드로 — 한 번만 센다
        self.session([self.marker_rec(0), asst(10, "m1"), asst(11, "m1"), asst(20, "m2")])
        row = self.collect()
        self.assertEqual(row["llm_calls"], 2)
        self.assertEqual(row["output_tokens"], 20)
        self.assertEqual(row["input_tokens"], 2 * (1 + 5 + 100))
        self.assertEqual(row["cache_read_tokens"], 200)
        self.assertEqual(row["cache_creation_tokens"], 10)

    def test_records_before_marker_excluded(self):
        before = asst(0, "m0", out=999)
        before["timestamp"] = "2026-10-19T09:59:00.000Z"
        self.session([before, self.marker_rec(0), asst(10, "m1")])
        self.assertEqual(self.collect()["output_tokens"], 10)

    def test_records_after_end_excluded(self):
        self.session([self.marker_rec(0), asst(10, "m1"), asst(700, "m2", out=999)])
        self.assertEqual(self.collect(end_sec=600)["output_tokens"], 10)

    def test_reviewer_subagents_and_phase(self):
        self.session([self.marker_rec(0), asst(10, "m1")])
        self.sub("s1", "r1", [{"type": "user", "timestamp": T(30)}, asst(40, "r1a", out=7), asst(90, "r1b", out=3)])
        self.sub("s1", "r2", [{"type": "user", "timestamp": T(200)}, asst(230, "r2a", out=5)])
        self.sub("s1", "x1", [{"type": "user", "timestamp": T(300)}, asst(310, "x1a", out=100)], agent_type="Explore")
        row = self.collect()
        self.assertEqual(row["review_calls"], 2)
        self.assertEqual(row["llm_calls"], 5)  # 메인 1 + 리뷰어 3 + 다른 서브에이전트 1
        self.assertEqual(row["output_tokens"], 10 + 7 + 3 + 5 + 100)
        self.assertEqual(row["phases"]["review"], {"duration_sec": 60 + 30, "input_tokens": 3 * 106, "output_tokens": 15})
        self.assertEqual(row["retry_count"], 1)

    def test_subagent_started_before_marker_excluded(self):
        self.session([self.marker_rec(0), asst(10, "m1")])
        old = [{"type": "user", "timestamp": "2026-10-19T09:00:00.000Z"}, asst(5, "o1", out=50)]
        self.sub("s1", "old", old)
        row = self.collect()
        self.assertEqual(row["review_calls"], 0)
        self.assertNotIn("phases", row)

    def test_stop_blocks_count_as_retry(self):
        self.session([self.marker_rec(0), asst(10, "m1"), stop_block(20), stop_block(40)])
        self.assertEqual(self.collect()["retry_count"], 2)

    def test_active_excludes_human_wait_and_ask(self):
        ask = asst(100, "m2", content=[{"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {}}])
        self.session([self.marker_rec(0), asst(10, "m1"), human(70),  # 60초 대기
                      ask, tool_result(250, "답", tool_use_id="q1"),  # 150초 대기
                      asst(260, "m3")])
        row = self.collect(end_sec=300)
        self.assertEqual(row["duration_sec"], 300)
        self.assertEqual(row["active_sec"], 300 - 60 - 150)

    def test_attachment_before_human_not_used_as_base(self):
        # 사람 메시지와 같은 시각에 attachment가 먼저 기록돼도 대기는 마지막 작업 기록부터 잰다
        att = {"type": "attachment", "timestamp": T(99)}
        self.session([self.marker_rec(0), asst(10, "m1"), att, human(100), asst(110, "m2")])
        self.assertEqual(self.collect(end_sec=120)["active_sec"], 120 - 90)

    def test_marker_in_other_session_not_mixed(self):
        self.session([self.marker_rec(0), asst(10, "m1")], sid="s1")
        self.session([asst(10, "z1", out=999)], sid="s2")
        self.assertEqual(self.collect()["output_tokens"], 10)

    def test_nonce_only_in_command_not_marker(self):
        # finish 명령 문자열에만 nonce가 있고 표지가 없는 기록은 고르지 않는다
        cmd = asst(10, "m1", content=[{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": f"finish {NONCE}"}}])
        self.session([cmd], sid="s0")
        self.assertIsNone(self.collect())

    def test_versions_and_models(self):
        self.session([self.marker_rec(0), asst(10, "m1", version="2.1.302"), asst(20, "m2", version="2.1.303")])
        row = self.collect()
        self.assertEqual(row["cc_version"], "2.1.303")
        self.assertEqual(row["model"], {"main": "claude-opus-5-5"})
        self.assertEqual(row["git_head"], "deadbee")
        self.assertEqual(row["metric_schema_version"], im.SCHEMA_VERSION)
        self.assertEqual((row["raw_id"], row["raw_count"], row["raw_size"]), (["2026-10-18_a.md"], 1, 1200))


class TestBResult(Base):
    def test_last_final_per_unit(self):
        rows = [
            {"event": "판정", "run_id": "r1", "unit": "a", "final": None},
            {"event": "판정", "run_id": "r1", "unit": "a", "final": "PASS"},
            {"event": "판정", "run_id": "r1", "unit": "b", "final": "FAIL"},
            {"event": "판정", "run_id": "r1", "unit": "b", "final": "FLAKY"},
            {"event": "판정", "run_id": "r1", "unit": "c", "final": "무효 반복 — 중단"},
            {"event": "판정", "run_id": "r2", "unit": "a", "final": "FAIL"},
            {"event": "준비", "run_id": "r1", "unit": "d"},
        ]
        p = self.write("h.jsonl", rows)
        n, res = im.b_result(["r1"], p)
        self.assertEqual(n, 3)
        self.assertEqual(res, {"pass": 1, "flaky": 1, "fail": 0, "invalid": 1})

    def test_no_runs(self):
        self.assertEqual(im.b_result([], "/nonexistent"), (0, {"pass": 0, "flaky": 0, "fail": 0, "invalid": 0}))


class TestCommands(Base):
    def test_start_prints_marker(self):
        raw = os.path.join(self.d, "2026-10-18_가.md")
        with open(raw, "w", encoding="utf-8") as f:
            f.write("한글 다섯자")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(im.main(["start", raw]), 0)
        line = buf.getvalue().splitlines()[0]
        self.assertTrue(line.startswith(im.MARK))
        d = json.loads(line[len(im.MARK):])
        self.assertEqual((d["raw"], d["raw_size"]), (["2026-10-18_가.md"], 6))  # 문자 수
        self.assertEqual(len(d["nonce"]), 12)

    def test_start_missing_raw(self):
        self.assertEqual(im.main(["start", os.path.join(self.d, "없음.md")]), 2)

    def test_finish_appends_and_dry_run(self):
        self.session([self.marker_rec(0), asst(10, "m1")])
        hist = os.path.join(self.d, "out.jsonl")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(im.main(["finish", NONCE, "--projects", self.d, "--history", hist, "--dry-run"]), 0)
            self.assertFalse(os.path.exists(hist))
            self.assertEqual(im.main(["finish", NONCE, "--projects", self.d, "--history", hist]), 0)
            self.assertEqual(im.main(["finish", NONCE, "--projects", self.d, "--history", hist]), 0)
        with open(hist, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)  # append — 덮어쓰지 않음
        self.assertEqual(json.loads(lines[0])["llm_calls"], 1)

    def test_finish_unknown_nonce(self):
        self.session([self.marker_rec(0)])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(im.main(["finish", "000000000000", "--projects", self.d, "--history", os.path.join(self.d, "o")]), 2)

    def test_projects_dir(self):
        self.assertTrue(im.projects_dir("/home/a_b/Obsidian").endswith("/-home-a-b-Obsidian"))


if __name__ == "__main__":
    unittest.main()
