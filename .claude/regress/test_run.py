#!/usr/bin/env python3
"""run.py validate 단위 테스트: python3 .claude/regress/test_run.py
임시 폴더에 가짜 볼트(시스템 파일 몇 개 + regress/)를 만들고, 일부러 틀린 케이스마다 해당 오류가 나오는지 본다."""
import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

REGISTRY = """\
components:
  schema: [CLAUDE.md]
  wiki-reviewer: [.claude/agents/wiki-reviewer.md]
  lint: [.claude/wiki_lint.py]
  wiki-ingest: [.claude/skills/wiki-ingest/**]
broad: [wiki-reviewer, schema]
failure_classes:
  recommendation-strength-loss: 권고 강도 변경
  stale-aggregate: 집계 줄 안 고침
"""

REVIEWER = """\
# 리뷰어
## 검토 항목
1. 하나
2. 둘
3. 셋
## 절차
1. 절차
"""

B_FM = {
    "id": "b-pos", "층": "B", "failure_class": "recommendation-strength-loss", "원인판정": "절차",
    "출처": '["[2026-10-05] ingest | 예시"]', "components": "[wiki-reviewer, wiki-ingest]", "files": "[]",
    "내릴 수 있나": "아니오", "precheck": "null", "private": "false", "수정한 페이지": "[wiki/요약-원문.md]",
}
B_BODY = """
## 기대
reviewer_items: [3]
must_detect:
- 권고 강도가 원문보다 약화됨
must_not_detect:
- 출처 간 상충

## 파일: raw/2026-10-01_원문.md
~~~~
You must pin versions.
~~~~

## 파일: wiki/요약-원문.md
~~~~
---
type: source
---
버전을 고정할 수 있다.
~~~~
"""

A_FM = {"id": "a-pos", "층": "A", "failure_class": "stale-aggregate", "원인판정": "규칙 공백",
        "출처": '["커밋 abc1234"]', "components": "[lint]", "runner": "lint", "private": "false"}
A_BODY = """
## 기대
must_output:
- "[하위폴더]"

## 파일: wiki/index.md
~~~~
x
~~~~
"""

HOOK_BODY = """
## 기대
receipt: 2
must_not_output:
- block

## 파일: hook/input.json
~~~~
{"agent_type": "wiki-reviewer", "agent_transcript_path": "hook/transcript.jsonl"}
~~~~

## 파일: hook/transcript.jsonl
~~~~
{"type": "assistant", "message": {"content": "REVIEW-RESULT: issues=2"}}
~~~~

## 파일: wiki/index.md
~~~~
x
~~~~
"""


def case_text(fm, body, drop=(), **over):
    fm = {**fm, **over}
    lines = [f"{k}: {v}" for k, v in fm.items() if k not in drop]
    return "---\n" + "\n".join(lines) + "\n---\n" + body


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.regress = os.path.join(self.root, ".claude", "regress")
        os.makedirs(os.path.join(self.regress, "cases"))
        os.makedirs(os.path.join(self.root, ".claude", "agents"))
        os.makedirs(os.path.join(self.root, ".claude", "skills", "wiki-ingest"))
        for p, t in (("CLAUDE.md", "# 규칙\n"), (".claude/wiki_lint.py", "#\n"),
                     (".claude/agents/wiki-reviewer.md", REVIEWER), (".claude/skills/wiki-ingest/SKILL.md", "#\n"),
                     (".claude/regress/registry.yaml", REGISTRY)):
            self.write(p, t)

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rel, text):
        with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def add(self, cid, text):
        self.write(f".claude/regress/cases/{cid}.md", text)

    def errors(self):
        return run.validate(self.regress, self.root)

    def assertError(self, needle):
        errors, _ = self.errors()
        self.assertTrue(any(needle in e for e in errors), f"'{needle}' 없음: {errors}")

    def add_valid(self):
        self.add("b-pos", case_text(B_FM, B_BODY))
        self.add("a-pos", case_text(A_FM, A_BODY))

    # 정상
    def test_valid_cases(self):
        self.add_valid()
        self.add("b-neg", case_text(B_FM, B_BODY.replace("reviewer_items: [3]", "reviewer_items: []")
                                    .replace("must_detect:\n- 권고 강도가 원문보다 약화됨\n", ""), id="b-neg"))
        errors, warns = self.errors()
        self.assertEqual(errors, [])
        self.assertEqual(warns, [])

    def test_readme_example_valid(self):
        readme = run.read(os.path.join(run.REGRESS, "README.md"))
        self.add("권고강도-보존", readme.split("````markdown\n", 1)[1].split("\n````", 1)[0] + "\n")
        self.assertEqual(self.errors(), ([], []))

    def test_usage_documents_run_a(self):
        # README 사용법과 docstring의 `run.py A` 줄이 빠지거나 어긋나지 않게
        line = "python3 .claude/regress/run.py A [<case> ...] [--root DIR] [--lint FILE]"
        readme = run.read(os.path.join(run.REGRESS, "README.md"))
        usage = readme.split("## 사용법\n", 1)[1].split("```\n", 1)[1].split("```", 1)[0]
        self.assertIn(line, usage)
        self.assertIn(line, run.__doc__)

    def test_usage_documents_run_b(self):
        # README 사용법·docstring·사용법 오류 문구의 `run.py B` 줄이 빠지거나 서로 어긋나지 않게
        line = "python3 .claude/regress/run.py B --select [--files <경로> ...] [--since <REF>] [--root DIR]"
        readme = run.read(os.path.join(run.REGRESS, "README.md"))
        usage = readme.split("## 사용법\n", 1)[1].split("```\n", 1)[1].split("```", 1)[0]
        self.assertIn(line, usage)
        self.assertIn(line, run.__doc__)
        self.assertIn(line.split("run.py ", 1)[1], run.B_USAGE)

    def test_usage_documents_run_b_modes(self):
        # 세션이 README만 보고 --prepare·--judge를 부른다 — 세 곳 중 한 곳만 바뀌면 사용법이 서로 어긋난다
        readme = run.read(os.path.join(run.REGRESS, "README.md"))
        usage = readme.split("## 사용법\n", 1)[1].split("```\n", 1)[1].split("```", 1)[0]
        for line in ("python3 .claude/regress/run.py B --prepare <run-id> [<유닛> ...] [--files <경로> ... | --since <REF>] "
                     "[--root DIR] [--base DIR]",
                     "python3 .claude/regress/run.py B --judge <run-id> <볼트> [<리뷰어 출력 파일> <transcript>] [--projects DIR] "
                     "[--detect 예|아니오] [--not-detect 없음|있음] [--root DIR] [--base DIR]"):
            with self.subTest(line=line.split()[3]):
                self.assertIn(line, usage)
                self.assertIn(line, run.__doc__)
                self.assertIn(line.split("run.py ", 1)[1], run.B_USAGE)

    def test_readme_b_run_section(self):
        # 리뷰어를 부르는 것은 세션이다 — 세션이 할 일 순서(준비 → 리뷰어 → 판정 → 대조 → 재실행)가 README에 있어야 따라 한다
        readme = run.read(os.path.join(run.REGRESS, "README.md"))
        self.assertIn("\n## B층 실행\n", readme)
        section = readme.split("\n## B층 실행\n", 1)[1].split("\n## ", 1)[0]
        at = [section.index(w) for w in ("--prepare", "wiki-reviewer", "--judge", "세션 대조 필요", "--detect", "재실행 요청문")]
        self.assertEqual(at, sorted(at))

    def test_empty_suite(self):
        self.assertEqual(self.errors(), ([], []))

    def test_parse_files_and_variants(self):
        body = B_BODY + "\n## 변형: 영어-should\n### 파일: raw/2026-10-01_원문.md\n~~~~\nYou should pin.\n~~~~\n"
        c = run.parse_case(case_text(B_FM, body))
        self.assertEqual(c["기대"]["reviewer_items"], [3])
        self.assertEqual(c["files"]["raw/2026-10-01_원문.md"], "You must pin versions.\n")
        self.assertTrue(c["files"]["wiki/요약-원문.md"].startswith("---\ntype: source\n---\n"))
        self.assertEqual(c["variants"]["영어-should"]["files"], {"raw/2026-10-01_원문.md": "You should pin.\n"})

    # 필수 칸·허용 값
    def test_missing_required(self):
        self.add("b-pos", case_text(B_FM, B_BODY, drop=("출처",)))
        self.assertError("필수 칸 없음: 출처")

    def test_unknown_key(self):
        self.add("b-pos", case_text(B_FM, B_BODY, 영향="[x]"))
        self.assertError("알 수 없는 칸: 영향")

    def test_id_mismatch(self):
        self.add("other", case_text(B_FM, B_BODY))
        self.assertError("파일 이름과 다름")

    def test_bad_layer(self):
        self.add("b-pos", case_text(B_FM, B_BODY, 층="C"))
        self.assertError("층은 A | B")

    def test_failure_class_not_registered(self):
        self.add("b-pos", case_text(B_FM, B_BODY, failure_class="made-up"))
        self.assertError("허용 목록에 없음: made-up")

    def test_bad_cause(self):
        self.add("b-pos", case_text(B_FM, B_BODY, 원인판정="정답이 갈림"))
        self.assertError("원인판정은")

    def test_empty_source(self):
        self.add("b-pos", case_text(B_FM, B_BODY, 출처="[]"))
        self.assertError("출처가 비었음")

    def test_private_not_bool(self):
        self.add("b-pos", case_text(B_FM, B_BODY, private="yes"))
        self.assertError("private는 true | false")

    # 영향
    def test_component_not_registered(self):
        self.add("b-pos", case_text(B_FM, B_BODY, components="[wiki-reviewer:item-3]"))
        self.assertError("component가 registry에 없음: wiki-reviewer:item-3")

    def test_file_missing(self):
        self.add("b-pos", case_text(B_FM, B_BODY, files="[.claude/없는파일.md]"))
        self.assertError("files의 경로가 없음")

    def test_no_target(self):
        self.add("b-pos", case_text(B_FM, B_BODY, components="[]"))
        self.assertError("둘 다 비었음")

    # 내릴 수 있나·precheck
    def test_partial_without_precheck(self):
        self.add("b-pos", case_text(B_FM, B_BODY, **{"내릴 수 있나": "부분"}))
        self.assertError("precheck 없음")

    def test_precheck_missing_case(self):
        self.add("b-pos", case_text(B_FM, B_BODY, **{"내릴 수 있나": "부분", "precheck": "a-none"}))
        self.assertError("precheck 케이스가 없음")

    def test_precheck_must_be_a(self):
        self.add_valid()
        self.add("b2", case_text(B_FM, B_BODY, id="b2", **{"내릴 수 있나": "부분", "precheck": "b-pos"}))
        self.assertError("A층 케이스여야 함")

    def test_partial_with_a_precheck_ok(self):
        self.add_valid()
        self.add("b2", case_text(B_FM, B_BODY, id="b2", **{"내릴 수 있나": "부분", "precheck": "a-pos"}))
        self.assertEqual(self.errors()[0], [])

    def test_precheck_without_partial(self):
        self.add_valid()
        self.add("b2", case_text(B_FM, B_BODY, id="b2", precheck="a-pos"))
        self.assertError("부분일 때만")

    def test_demote_yes_warns(self):
        self.add("b-pos", case_text(B_FM, B_BODY, **{"내릴 수 있나": "예"}))
        errors, warns = self.errors()
        self.assertEqual(errors, [])
        self.assertTrue(any("A층으로 옮기고" in w for w in warns))

    def test_b_requires_demote(self):
        self.add("b-pos", case_text(B_FM, B_BODY, drop=("내릴 수 있나",)))
        self.assertError("내릴 수 있나는")

    def test_a_rejects_demote(self):
        self.add("a-pos", case_text(A_FM, A_BODY, **{"내릴 수 있나": "예"}))
        self.assertError("A층 케이스에는")

    # 기대
    def test_missing_expect(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("## 기대", "## 메모")))
        self.assertError("기대 칸 없음")

    def test_positive_needs_must_detect(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("must_detect:\n- 권고 강도가 원문보다 약화됨\n", "")))
        self.assertError("must_detect 비었음")

    def test_negative_needs_must_not_detect(self):
        body = B_BODY.replace("reviewer_items: [3]", "reviewer_items: []").replace("must_not_detect:\n- 출처 간 상충\n", "")
        self.add("b-pos", case_text(B_FM, body))
        self.assertError("must_not_detect 비었음")

    def test_reviewer_item_range(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("[3]", "[9]")))
        self.assertError("범위 밖: 9")

    # any-of — 리뷰어가 같은 결함을 실행마다 다른 항목(2·3)으로 보고하는 케이스용 (2026-10-19)
    def test_reviewer_items_any_alone_ok(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("reviewer_items: [3]", "reviewer_items_any: [2, 3]")))
        self.assertEqual(self.errors()[0], [])

    def test_reviewer_items_any_range(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("reviewer_items: [3]", "reviewer_items_any: [2, 9]")))
        self.assertError("reviewer_items_any 범위 밖: 9")

    def test_reviewer_items_any_not_empty(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("reviewer_items: [3]", "reviewer_items: []\nreviewer_items_any: []")))
        self.assertError("reviewer_items_any가 비었음")

    def test_reviewer_items_any_is_positive(self):
        body = B_BODY.replace("reviewer_items: [3]", "reviewer_items_any: [2, 3]").replace("must_detect:\n- 권고 강도가 원문보다 약화됨\n", "")
        self.add("b-pos", case_text(B_FM, body))
        self.assertError("must_detect 비었음")

    def test_reviewer_items_both_missing(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("reviewer_items: [3]\n", "")))
        self.assertError("reviewer_items 없음")

    def test_expect_key_by_layer(self):
        self.add("a-pos", case_text(A_FM, A_BODY.replace("must_output", "reviewer_items")))
        self.assertError("알 수 없는 키: reviewer_items")

    def test_a_needs_output(self):
        self.add("a-pos", case_text(A_FM, A_BODY.replace('must_output:\n- "[하위폴더]"', "must_output: []")))
        self.assertError("둘 다 비었음")

    def test_a_runner_required(self):
        self.add("a-pos", case_text(A_FM, A_BODY, drop=("runner",)))
        self.assertError("A층 runner는")

    def test_b_rejects_runner(self):
        self.add("b-pos", case_text(B_FM, B_BODY, runner="lint"))
        self.assertError("B층 케이스에는 runner를")

    def test_hook_runner_valid(self):
        self.add("a-hook", case_text(A_FM, HOOK_BODY, id="a-hook", runner="hook-review-done"))
        self.assertEqual(self.errors(), ([], []))

    def test_hook_runner_null_receipt_ok(self):
        self.add("a-hook", case_text(A_FM, HOOK_BODY.replace("receipt: 2", "receipt: null"), id="a-hook",
                                     runner="hook-review-done"))
        self.assertEqual(self.errors(), ([], []))

    def test_hook_runner_needs_input(self):
        body = HOOK_BODY.replace("## 파일: hook/input.json", "## 파일: hook/other.json")
        self.add("a-hook", case_text(A_FM, body, id="a-hook", runner="hook-review-done"))
        self.assertError("필요한 파일 칸 없음: hook/input.json")

    def test_hook_runner_needs_receipt(self):
        self.add("a-hook", case_text(A_FM, HOOK_BODY.replace("receipt: 2\n", ""), id="a-hook", runner="hook-review-done"))
        self.assertError("receipt 없음")

    def test_hook_runner_receipt_type(self):
        self.add("a-hook", case_text(A_FM, HOOK_BODY.replace("receipt: 2", "receipt: 많음"), id="a-hook",
                                     runner="hook-review-done"))
        self.assertError("receipt는 정수 또는 null")

    def test_hook_dir_only_for_hook_runner(self):
        self.add("a-pos", case_text(A_FM, A_BODY + "\n## 파일: hook/input.json\n~~~~\n{}\n~~~~\n"))
        self.assertError("파일 경로는")

    def test_receipt_only_for_hook_runner(self):
        self.add("a-pos", case_text(A_FM, A_BODY.replace("## 기대\n", "## 기대\nreceipt: 0\n")))
        self.assertError("알 수 없는 키: receipt")

    # 파일 칸
    def test_file_path_outside(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("## 파일: raw/2026", "## 파일: .claude/2026")))
        self.assertError("파일 경로는")

    def test_file_subfolder(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("## 파일: raw/2026", "## 파일: raw/sub/2026")))
        self.assertError("하위 폴더 없음")

    def test_b_needs_wiki_page(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("## 파일: wiki/요약-원문.md", "## 파일: raw/요약-원문.md")))
        self.assertError("wiki/ 파일 칸이 없음")

    def test_unclosed_fence(self):
        self.add("b-pos", case_text(B_FM, B_BODY.rstrip().rsplit("~~~~", 1)[0]))
        self.assertError("닫히지 않음")

    def test_duplicate_file(self):
        self.add("b-pos", case_text(B_FM, B_BODY + "\n## 파일: wiki/요약-원문.md\n~~~~\nx\n~~~~\n"))
        self.assertError("파일 칸 중복")

    def test_text_outside_fence(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("~~~~\nYou must", "설명\n~~~~\nYou must")))
        self.assertError("울타리")

    def test_variant_checked(self):
        body = B_BODY + "\n## 변형: v2\n### 파일: raw/sub/x.md\n~~~~\nx\n~~~~\n"
        self.add("b-pos", case_text(B_FM, body))
        self.assertError("변형 v2: 파일 경로는")

    # 수정한 페이지 — 요청문에 "수정한 페이지"로 넘길 것과 비교 대상 페이지를 가른다 (2026-10-19 결정)
    def test_reviewed_pages_required_for_b(self):
        # 없으면 --prepare가 요청문을 만들 수 없다
        self.add("b-pos", case_text(B_FM, B_BODY, drop=("수정한 페이지",)))
        self.assertError("B층인데 수정한 페이지 없음")
        self.add("b-pos", case_text(B_FM, B_BODY, **{"수정한 페이지": "[]"}))
        self.assertError("B층인데 수정한 페이지 없음")

    def test_reviewed_page_must_be_case_wiki_file(self):
        # 미니 볼트에 없는 페이지나 raw를 넘기면 리뷰어가 읽을 수 없다
        for bad in ("wiki/없는.md", "raw/2026-10-01_원문.md"):
            with self.subTest(bad=bad):
                self.add("b-pos", case_text(B_FM, B_BODY, **{"수정한 페이지": f"[{bad}]"}))
                self.assertError(f"수정한 페이지는 이 케이스의 wiki/ 파일 칸이어야 함: {bad}")

    def test_a_layer_has_no_reviewed_pages(self):
        self.add("a-pos", case_text(A_FM, A_BODY, **{"수정한 페이지": "[wiki/index.md]"}))
        self.assertError("A층 케이스에는 수정한 페이지를 두지 않음")

    def test_variant_reviewed_pages(self):
        # 변형은 ### 수정한 페이지로 바꿀 수 있고(없으면 본 케이스 것), 본 케이스 파일 칸도 고를 수 있다
        body = B_BODY + ("\n## 변형: v2\n### 수정한 페이지\n- wiki/요약-원문.md\n- wiki/index.md\n"
                         "### 파일: wiki/index.md\n~~~~\nx\n~~~~\n")
        c = run.parse_case(case_text(B_FM, body))
        self.assertEqual(c["variants"]["v2"]["수정한 페이지"], ["wiki/요약-원문.md", "wiki/index.md"])
        self.add("b-pos", case_text(B_FM, body))
        self.assertEqual(self.errors(), ([], []))
        self.add("b-pos", case_text(B_FM, body.replace("- wiki/index.md\n", "- wiki/없는.md\n")))
        self.assertError("변형 v2: 수정한 페이지는 이 케이스의 wiki/ 파일 칸이어야 함: wiki/없는.md")

    def test_reviewed_pages_section_shape(self):
        # 본 케이스는 frontmatter에만 — 두 곳에 두면 어느 쪽이 맞는지 모호하다
        with self.assertRaisesRegex(ValueError, "수정한 페이지 칸은 변형 안에서만"):
            run.parse_case(case_text(B_FM, "\n## 수정한 페이지\n- wiki/요약-원문.md\n" + B_BODY))
        with self.assertRaisesRegex(ValueError, "- <경로>"):
            run.parse_case(case_text(B_FM, B_BODY + "\n## 변형: v2\n### 수정한 페이지\nwiki/요약-원문.md\n"
                                     "### 파일: raw/2026-10-01_원문.md\n~~~~\nx\n~~~~\n"))

    def test_variant_expect_checked(self):
        body = B_BODY + "\n## 변형: v2\n### 기대\nreviewer_items: [3]\n### 파일: raw/2026-10-01_원문.md\n~~~~\nx\n~~~~\n"
        self.add("b-pos", case_text(B_FM, body))
        self.assertError("변형 v2: 양성 케이스")

    # 파서·registry
    def test_bad_yaml_shape(self):
        self.add("b-pos", case_text(B_FM, B_BODY.replace("reviewer_items: [3]", "reviewer_items: [3")))
        self.assertError("닫는 ]")

    def test_registry_broad_unknown(self):
        self.write(".claude/regress/registry.yaml", REGISTRY.replace("broad: [wiki-reviewer, schema]", "broad: [nope]"))
        self.assertError("broad의 nope")

    def test_registry_dead_glob_warns(self):
        self.write(".claude/regress/registry.yaml", REGISTRY.replace("[.claude/wiki_lint.py]", "[.claude/gone.py]"))
        errors, warns = self.errors()
        self.assertEqual(errors, [])
        self.assertTrue(any("맞는 파일 없음 .claude/gone.py" in w for w in warns))

    def test_registry_bad_shape(self):
        self.write(".claude/regress/registry.yaml", REGISTRY.replace("broad: [wiki-reviewer, schema]", "broad: x"))
        self.assertError("broad가 목록이 아님")

    def test_parse_simple(self):
        d = run.parse_simple('# 주석\na: [x, "y, z", 3]\nb:\n  k: v\nc:\n- 1\n- 둘: 셋\nd: null\ne: true\n')
        self.assertEqual(d, {"a": ["x", "y, z", 3], "b": {"k": "v"}, "c": [1, "둘: 셋"], "d": None, "e": True})


class MkvaultTest(ValidateTest):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.root, ".claude", "rules"))
        self.write(".claude/rules/페이지-형식.md", "# 형식\n")
        self.base = os.path.join(self.root, "tmp-vaults")
        self.add_valid()
        self.add("b-var", case_text(B_FM, B_BODY + "\n## 변형: 강하게\n### 파일: raw/2026-10-01_원문.md\n~~~~\nYou can pin.\n~~~~\n",
                                    id="b-var"))

    def mk(self, cid, **kw):
        return run.mkvault(cid, run_id="r1", regress=self.regress, root=self.root, base=self.base, **kw)

    def test_session_vault(self):
        v = self.mk("b-pos")
        self.assertEqual(v, os.path.join(self.base, "r1", "b-pos"))
        for rel in ("CLAUDE.md", ".claude/agents/wiki-reviewer.md", ".claude/rules/페이지-형식.md",
                    "wiki/요약-원문.md", "raw/2026-10-01_원문.md"):
            self.assertTrue(os.path.isfile(os.path.join(v, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(v, ".claude", "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(v, ".claude", "regress")))   # 기대가 든 케이스는 넣지 않음

    def test_headless_vault(self):
        v = self.mk("b-pos", headless=True)
        with open(os.path.join(v, ".claude", "settings.json"), encoding="utf-8") as f:
            self.assertEqual(list(run.json.load(f)["hooks"]), ["SubagentStop"])
        self.assertTrue(os.path.isfile(os.path.join(v, ".claude", "wiki_lint.py")))

    def test_variant_overrides_file(self):
        v = self.mk("b-var", variant="강하게")
        with open(os.path.join(v, "raw", "2026-10-01_원문.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "You can pin.\n")
        self.assertTrue(os.path.isfile(os.path.join(v, "wiki", "요약-원문.md")))

    def test_rejects_a_layer_and_reuse(self):
        with self.assertRaises(ValueError):
            self.mk("a-pos")
        self.mk("b-pos")
        with self.assertRaises(ValueError):
            self.mk("b-pos")


# 가짜 검사기: 임시 폴더(cwd)의 wiki/index.md를 그대로 출력 — 파일 칸 내용으로 검사기 출력을 정한다
FAKE_LINT = "print(open('wiki/index.md', encoding='utf-8').read())\n"
A_PASS_BODY = A_BODY.replace("~~~~\nx\n~~~~", "~~~~\n[하위폴더]\n~~~~")


class RunATest(ValidateTest):
    def setUp(self):
        super().setUp()
        self.write(".claude/wiki_lint.py", FAKE_LINT)

    def a(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run.main_a(list(args), regress=self.regress, root=self.root)
        return code, buf.getvalue().splitlines()

    def assertNoResult(self, lines):
        self.assertFalse([x for x in lines if x.startswith(("PASS ", "FAIL ", "A층:"))], lines)

    # 결과 줄·요약 줄 형식과 종료 코드 0 — B층 케이스는 실행 수에 들지 않아야 한다
    def test_a_all_pass(self):
        self.add("a-pos", case_text(A_FM, A_PASS_BODY))
        self.add("b-pos", case_text(B_FM, B_BODY))
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertTrue(lines[-1].startswith("A층: 실행 1 · PASS 1 · FAIL 0 · ") and lines[-1].endswith("s"), lines)
        self.assertEqual(code, 0)

    # 기대가 어긋나면 FAIL 줄에 이유가 붙고 종료 코드 1
    def test_a_fail_exit1(self):
        self.add("a-pos", case_text(A_FM, A_BODY))
        code, lines = self.a()
        self.assertTrue(any(x.startswith("FAIL a-pos — ") for x in lines), lines)
        self.assertTrue(lines[-1].startswith("A층: 실행 1 · PASS 0 · FAIL 1 · "), lines)
        self.assertEqual(code, 1)

    def a_case(self, items, content, not_items=()):
        """must_output·must_not_output 항목과 wiki/index.md 내용으로 a-pos 케이스를 쓴다. 빈 목록은 칸을 쓰지 않는다."""
        exp = "".join(f"{key}:\n" + "".join(f'- "{m}"\n' for m in ms)
                      for key, ms in (("must_output", items), ("must_not_output", not_items)) if ms)
        body = f"\n## 기대\n{exp}\n## 파일: wiki/index.md\n~~~~\n{content}\n~~~~\n"
        self.add("a-pos", case_text(A_FM, body))

    # 이유에 빠진 항목만 적어야 무엇이 사라졌는지 바로 안다 — 나온 항목은 이유에 없어야 한다
    def test_a_must_output_reason_lists_missing(self):
        self.a_case(["[하위폴더]", "[없는조각]"], "[하위폴더]")
        code, lines = self.a()
        self.assertIn("FAIL a-pos — must_output 없음: [없는조각]", lines)
        self.assertEqual(code, 1)

    # 첫 빠진 항목에서 멈추면 나머지 회귀를 가린다 — 빠진 항목을 모두 적는다
    def test_a_must_output_all_missing_listed(self):
        self.a_case(["[가]", "[나]"], "x")
        code, lines = self.a()
        fail = [x for x in lines if x.startswith("FAIL a-pos — ")]
        self.assertEqual(len(fail), 1, lines)
        self.assertIn("[가]", fail[0])
        self.assertIn("[나]", fail[0])
        self.assertEqual(code, 1)

    # 기준은 "어느 한 줄의 부분 문자열" — 두 줄에 걸친 일치는 통과시키지 않는다
    def test_a_must_output_per_line(self):
        self.a_case(["앞 뒤"], "앞\n뒤")
        code, lines = self.a()
        self.assertTrue(any(x.startswith("FAIL a-pos — ") for x in lines), lines)
        self.assertEqual(code, 1)
        self.a_case(["앞 뒤"], "xx 앞 뒤 yy")
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertEqual(code, 0)

    # 기준은 검사기 stdout — stderr에만 나온 항목은 통과시키지 않는다
    def test_a_must_output_stdout_only(self):
        self.write(".claude/wiki_lint.py", "import sys\nprint('[하위폴더]', file=sys.stderr)\n")
        self.a_case(["[하위폴더]"], "x")
        code, lines = self.a()
        self.assertIn("FAIL a-pos — must_output 없음: [하위폴더]", lines)
        self.assertEqual(code, 1)

    # 음성 기대를 읽지 않으면 검사기가 막아야 할 줄을 내도 PASS가 된다 — 나온 항목을 이유에 적고 FAIL
    def test_a_must_not_output_hit_fails(self):
        self.a_case([], "xx [하위폴더] yy", not_items=["[하위폴더]"])
        code, lines = self.a()
        self.assertIn("FAIL a-pos — must_not_output 나옴: [하위폴더]", lines)
        self.assertEqual(code, 1)

    # 이유에는 실제로 나온 항목만 — 안 나온 항목까지 적으면 무엇이 샜는지 흐려진다
    def test_a_must_not_output_reason_lists_only_hits(self):
        self.a_case([], "[가]", not_items=["[가]", "[나]"])
        code, lines = self.a()
        fail = [x for x in lines if x.startswith("FAIL a-pos — ")]
        self.assertEqual(len(fail), 1, lines)
        self.assertIn("[가]", fail[0])
        self.assertNotIn("[나]", fail[0])
        self.assertEqual(code, 1)

    # 음성 항목이 안 나오면 PASS — 음성 기대만 있는 케이스도 통과할 수 있어야 한다
    def test_a_must_not_output_absent_passes(self):
        self.a_case([], "x", not_items=["[없는조각]"])
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertEqual(code, 0)

    # 기준은 검사기 stdout — stderr에만 나온 항목으로 FAIL하지 않는다
    def test_a_must_not_output_stdout_only(self):
        self.write(".claude/wiki_lint.py", "import sys\nprint('[하위폴더]', file=sys.stderr)\n")
        self.a_case([], "x", not_items=["[하위폴더]"])
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertEqual(code, 0)

    # 양성·음성이 함께 어긋나면 한쪽 이유만 적어 다른 회귀를 가리지 않는다 — 한 FAIL 줄에 둘 다
    def test_a_both_reasons(self):
        self.a_case(["[없는조각]"], "[하위폴더]", not_items=["[하위폴더]"])
        code, lines = self.a()
        fail = [x for x in lines if x.startswith("FAIL a-pos — ")]
        self.assertEqual(len(fail), 1, lines)
        self.assertIn("must_output 없음: [없는조각]", fail[0])
        self.assertIn("must_not_output 나옴: [하위폴더]", fail[0])
        self.assertEqual(code, 1)

    # 변형은 따로 한 번 더 돌고 id가 <case>--<변형>
    def test_a_variant_id(self):
        self.add("a-pos", case_text(A_FM, A_PASS_BODY + "\n## 변형: v1\n### 파일: wiki/index.md\n~~~~\n[하위폴더] v1\n~~~~\n"))
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertIn("PASS a-pos--v1", lines)
        self.assertTrue(lines[-1].startswith("A층: 실행 2 · PASS 2 · FAIL 0 · "), lines)
        self.assertEqual(code, 0)

    # 변형 칸이 본 칸을 덮어써야 한다 — 본 파일로 돌리면 v1이 FAIL. 변형 자기 기대가 본 기대 대신 쓰이는 것도 고정
    def test_a_variant_overrides_file(self):
        body = (A_PASS_BODY + "\n## 변형: v1\n### 기대\nmust_output:\n- \"[변형만]\"\nmust_not_output:\n- \"[하위폴더]\"\n"
                "### 파일: wiki/index.md\n~~~~\n[변형만]\n~~~~\n")
        self.add("a-pos", case_text(A_FM, body))
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertIn("PASS a-pos--v1", lines)
        self.assertEqual(code, 0)

    # 변형에 ### 기대가 없으면 본 기대를 물려받아야 한다 — 빈 기대(exp or {})가 되면 늘 PASS라 회귀를 놓친다
    def test_a_variant_inherits_expect(self):
        self.add("a-pos", case_text(A_FM, A_PASS_BODY + "\n## 변형: v1\n### 파일: wiki/index.md\n~~~~\nx\n~~~~\n"))
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertIn("FAIL a-pos--v1 — must_output 없음: [하위폴더]", lines)
        self.assertEqual(code, 1)

    # 변형이 적지 않은 본 파일 칸은 남아야 한다 — 변형 칸만 펼치면 wiki/index.md가 없어 가짜 검사기가 죽고 FAIL
    def test_a_variant_keeps_base_files(self):
        body = A_PASS_BODY + "\n## 파일: wiki/a.md\n~~~~\nx\n~~~~\n\n## 변형: v1\n### 파일: wiki/a.md\n~~~~\ny\n~~~~\n"
        self.add("a-pos", case_text(A_FM, body))
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertIn("PASS a-pos--v1", lines)
        self.assertEqual(code, 0)

    # 형식이 깨진 케이스를 조용히 건너뛰면 회귀를 놓친다 — FAIL로 세고 parse_case 이유를 적는다
    def test_a_bad_format_counts_fail(self):
        self.add("a-bad", case_text(A_FM, A_BODY.rstrip().rsplit("~~~~", 1)[0], id="a-bad"))
        code, lines = self.a()
        self.assertIn("FAIL a-bad — 파일 칸 울타리가 닫히지 않음: wiki/index.md", lines)
        self.assertTrue(lines[-1].startswith("A층: 실행 1 · PASS 0 · FAIL 1 · "), lines)
        self.assertEqual(code, 1)

    # 지정한 케이스가 없으면 오타를 PASS로 착각하지 않게 결과 없이 2
    def test_a_unknown_case_exit2(self):
        self.add_valid()
        code, lines = self.a("없는-케이스")
        self.assertEqual(code, 2)
        self.assertNoResult(lines)

    # B층 케이스는 A층 러너가 돌릴 수 없다 — 지정하면 2
    def test_a_b_case_exit2(self):
        self.add_valid()
        code, lines = self.a("b-pos")
        self.assertEqual(code, 2)
        self.assertNoResult(lines)

    # 옵션 값 없음·모르는 옵션은 사용법 오류 2
    def test_a_bad_option_exit2(self):
        self.add_valid()
        for args in (("--lint",), ("--root",), ("--x",), ("a-pos", "--x")):
            code, lines = self.a(*args)
            self.assertEqual(code, 2, args)
            self.assertNoResult(lines)


# 가짜 hook 검사기: stdin JSON의 "want"를 자기 옆 .lint_state.json에 쓰고(--review-done일 때),
# 받은 경로 값과 "say" 줄을 출력한다 — 케이스의 input.json으로 영수증과 stdout을 정한다
FAKE_HOOK_LINT = """\
import json, os, sys
d = json.loads(sys.stdin.read())
if "--review-done" in sys.argv and "want" in d:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lint_state.json"), "w") as f:
        json.dump(d["want"], f)
for k in ("agent_transcript_path",):
    if k in d:
        print(f"abs={os.path.isabs(d[k])} exists={os.path.exists(d[k])} path={d[k]}")
if "nest" in d:
    print(f"nest_abs={os.path.isabs(d['nest']['p'][0])}")
if "x" in d:
    print(f"x={d['x']} n={d['n']}")
for line in d.get("say", []):
    print(line)
"""


class RunAHookTest(ValidateTest):
    a = RunATest.a

    def setUp(self):
        super().setUp()
        self.write(".claude/wiki_lint.py", FAKE_HOOK_LINT)

    def hook_case(self, input_json, receipt, must_output=(), must_not_output=()):
        """input.json 내용·기대 receipt·stdout 기대로 a-hook 케이스를 쓴다 (transcript는 HOOK_BODY 것)."""
        exp = f"receipt: {receipt}\n" + "".join(
            f"{key}:\n" + "".join(f'- "{m}"\n' for m in ms)
            for key, ms in (("must_output", must_output), ("must_not_output", must_not_output)) if ms)
        body = HOOK_BODY.replace("receipt: 2\nmust_not_output:\n- block\n", exp).replace(
            '{"agent_type": "wiki-reviewer", "agent_transcript_path": "hook/transcript.jsonl"}', input_json)
        self.add("a-hook", case_text(A_FM, body, id="a-hook", runner="hook-review-done"))

    # 영수증이 기대와 같으면 PASS
    def test_a_hook_receipt_match_passes(self):
        self.hook_case('{"want": {"reviews": {"k": 2}}}', 2)
        code, lines = self.a()
        self.assertIn("PASS a-hook", lines)
        self.assertEqual(code, 0)

    # receipt를 읽지 않으면 영수증이 틀려도 PASS가 된다 — 기대와 실제를 함께 적고 FAIL
    def test_a_hook_receipt_mismatch_fails(self):
        self.hook_case('{"want": {"reviews": {"k": 1}}}', 2)
        code, lines = self.a()
        self.assertIn("FAIL a-hook — receipt 기대 2, 실제 1", lines)
        self.assertEqual(code, 1)

    # 상태 파일이 없으면 영수증은 null — null 기대는 PASS, 숫자 기대는 FAIL
    def test_a_hook_receipt_null_when_no_state(self):
        self.hook_case('{}', "null")
        code, lines = self.a()
        self.assertIn("PASS a-hook", lines)
        self.assertEqual(code, 0)
        self.hook_case('{}', 2)
        code, lines = self.a()
        self.assertIn("FAIL a-hook — receipt 기대 2, 실제 null", lines)
        self.assertEqual(code, 1)

    # reviews가 비면 영수증이 남지 않은 것 — null
    def test_a_hook_receipt_null_when_reviews_empty(self):
        self.hook_case('{"want": {"reviews": {}}}', "null")
        code, lines = self.a()
        self.assertIn("PASS a-hook", lines)
        self.assertEqual(code, 0)

    # 영수증이 여럿이면 어느 것이 이번 검토인지 정할 수 없다 — 하나를 골라 PASS하지 않고 FAIL
    def test_a_hook_receipt_multiple_fails(self):
        self.hook_case('{"want": {"reviews": {"a": 1, "b": 2}}}', 1)
        code, lines = self.a()
        self.assertIn("FAIL a-hook — reviews 값이 여럿: 1, 2", lines)
        self.assertEqual(code, 1)

    # cwd가 임시 폴더라 상대 경로도 우연히 풀린다 — 검사기가 받은 값이 절대 경로인지 직접 본다.
    # 절대 경로에도 hook/transcript.jsonl이 들어가므로 음성 기대는 "path=hook/"(값 맨 앞)으로 본다
    def test_a_hook_paths_absolute(self):
        self.hook_case('{"agent_transcript_path": "hook/transcript.jsonl", "nest": {"p": ["hook/transcript.jsonl"]}, '
                       '"x": "wiki/a.md", "n": 3}', "null",
                       must_output=["abs=True exists=True", "nest_abs=True", "x=wiki/a.md n=3"],
                       must_not_output=["path=hook/"])
        code, lines = self.a()
        self.assertIn("PASS a-hook", lines)
        self.assertEqual(code, 0)

    # input.json이 JSON이 아니면 경로를 바꿀 수 없다 — 조용히 원문을 넘기지 않고 FAIL
    def test_a_hook_bad_json_fails(self):
        self.hook_case('not json', "null")
        code, lines = self.a()
        self.assertTrue(any(x.startswith("FAIL a-hook — hook/input.json이 JSON이 아님") for x in lines), lines)
        self.assertEqual(code, 1)

    # hook 케이스도 stdout을 compare_output으로 본다 — receipt가 맞아도 막아야 할 줄이 나오면 FAIL
    def test_a_hook_stdout_rules(self):
        self.hook_case('{"want": {"reviews": {"k": 2}}, "say": ["block"]}', 2, must_not_output=["block"])
        code, lines = self.a()
        self.assertIn("FAIL a-hook — must_not_output 나옴: block", lines)
        self.assertEqual(code, 1)

    # 두 이유가 함께면 한 줄에 둘 다 — receipt 이유가 stdout 이유를 가리지 않는다
    def test_a_hook_both_reasons(self):
        self.hook_case('{"want": {"reviews": {"k": 1}}, "say": ["block"]}', 2, must_not_output=["block"])
        code, lines = self.a()
        self.assertIn("FAIL a-hook — receipt 기대 2, 실제 1; must_not_output 나옴: block", lines)
        self.assertEqual(code, 1)


# 가짜 격리 검사기: cwd의 파일 목록(상대 경로 정렬)과 CLAUDE.md 내용을 출력한 뒤,
# 자기 옆 .lint_state.json과 cwd의 wiki/index.md를 덮어쓴다 — 어디서 돌았고 무엇을 건드렸는지 드러낸다
FAKE_ISO_LINT = """\
import os
fs = sorted(os.path.relpath(os.path.join(d, n), ".") for d, _, ns in os.walk(".") for n in ns)
print("files=" + ",".join(fs))
print("claude=" + open("CLAUDE.md", encoding="utf-8").read().strip())
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lint_state.json"), "w") as f:
    f.write('{"reviews": {"x": 9}}')
os.makedirs("wiki", exist_ok=True)
with open("wiki/index.md", "w", encoding="utf-8") as f:
    f.write("overwritten")
"""


FAKE_GIT_LINT = """import os, subprocess
print(subprocess.run(["git", "-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True).stdout)
print("TODAY=" + os.environ.get("WIKI_LINT_TODAY", ""))
"""
GIT_BODY = """
## 기대
must_output:
- " M wiki/가.md"
- "?? wiki/나.md"
- "TODAY=2026-01-15"
must_not_output:
- "라.md"
- "CLAUDE.md"
- "wiki_lint.py"

## 파일: base/가.md
~~~~
옛 내용
~~~~

## 파일: base/라.md
~~~~
그대로
~~~~

## 파일: wiki/가.md
~~~~
새 내용
~~~~

## 파일: wiki/나.md
~~~~
새 페이지
~~~~
"""


class RunAGitTest(ValidateTest):
    a = RunATest.a

    def setUp(self):
        super().setUp()
        self.write(".claude/wiki_lint.py", FAKE_GIT_LINT)

    # base/는 커밋된 상태, wiki/ 칸은 그 위의 커밋 안 된 변경 — 검사기에는 바뀐·새 페이지만 보이고 오늘은 A_TODAY로 고정
    def test_lint_git_commits_base_then_overlays(self):
        self.add("a-git", case_text(A_FM, GIT_BODY, id="a-git", runner="lint-git"))
        code, lines = self.a()
        self.assertIn("PASS a-git", lines)
        self.assertEqual(code, 0)

    # base/는 lint-git에만 — 다른 runner에서는 경로 오류
    def test_base_only_for_lint_git(self):
        self.add("a-git", case_text(A_FM, GIT_BODY, id="a-git", runner="lint"))
        self.assertError("파일 경로는")

    # lint-git도 기대 칸 규칙은 lint와 같다 — receipt는 알 수 없는 키
    def test_lint_git_rejects_receipt(self):
        self.add("a-git", case_text(A_FM, GIT_BODY.replace("## 기대\n", "## 기대\nreceipt: 0\n"), id="a-git", runner="lint-git"))
        self.assertError("알 수 없는 키: receipt")


class RunAIsolationTest(ValidateTest):
    a = RunATest.a
    a_case = RunATest.a_case

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.root, "wiki"))
        os.makedirs(os.path.join(self.root, "raw"))
        for p, t in ((".claude/wiki_lint.py", FAKE_ISO_LINT), (".claude/sync_template.py", "#\n"),
                     (".claude/.lint_state.json", '{"keep": 1}'), ("wiki/index.md", "root-wiki"),
                     ("raw/2026-10-01_x.md", "root-raw")):
            self.write(p, t)

    def snapshot(self):
        """가짜 볼트의 .lint_state.json·wiki/·raw/ → {상대 경로: 바이트}."""
        out = {}
        for top in (".claude/.lint_state.json", "wiki", "raw"):
            base = os.path.join(self.root, top)
            paths = [base] if os.path.isfile(base) else [os.path.join(d, n) for d, _, ns in os.walk(base) for n in ns]
            for p in paths:
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.root)] = f.read()
        return out

    # 임시 폴더에는 파일 칸 + CLAUDE.md + 검사기만 — root 전체 복사·sync_template.py·root raw/가 따라오면 목록이 달라 FAIL
    def test_a_tmp_layout(self):
        self.a_case(["files=.claude/wiki_lint.py,CLAUDE.md,wiki/index.md", "claude=# 규칙"], "case-wiki")
        code, lines = self.a()
        self.assertIn("PASS a-pos", lines)
        self.assertEqual(code, 0)

    # 실행(변형 포함)마다 새 폴더를 볼트 밖에 만들고 끝나면 지운다 — rmtree를 빼거나 폴더를 같이 쓰면 FAIL
    def test_a_tmp_fresh_and_removed(self):
        self.add("a-pos", case_text(A_FM, A_BODY + "\n## 변형: v1\n### 파일: wiki/index.md\n~~~~\ny\n~~~~\n"))
        made, real = [], tempfile.mkdtemp

        def record(*args, **kw):
            made.append(real(*args, **kw))
            return made[-1]

        with mock.patch.object(run.tempfile, "mkdtemp", side_effect=record):
            self.a()
        self.assertEqual(len(made), 2, made)
        self.assertNotEqual(made[0], made[1])
        root = os.path.realpath(self.root) + os.sep
        for d in made:
            self.assertFalse(os.path.realpath(d).startswith(root), d)
            self.assertFalse(os.path.exists(d), d)

    # 실제 볼트의 상태 파일·wiki/·raw/는 그대로 — 가짜 검사기가 덮어쓰므로 cwd를 root로 두거나 root 검사기를 그 자리에서 돌리면 FAIL
    def test_a_root_unchanged(self):
        self.a_case(["files="], "case-wiki")
        before = self.snapshot()
        self.a()
        self.assertEqual(self.snapshot(), before)

    # --lint로 준 검사기를 써야 변이 검사가 된다 — 무시하고 root 검사기를 쓰면 alt-lint가 안 나와 FAIL
    def test_a_lint_option_used(self):
        alt = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, alt)
        path = os.path.join(alt, "lint.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("print('alt-lint')\n")
        self.a_case(["alt-lint"], "case-wiki")
        code, lines = self.a("--lint", path)
        self.assertIn("PASS a-pos", lines)
        self.assertEqual(code, 0)


B_REGISTRY = REGISTRY.replace("broad:", "  x: [src/x/**]\n  y: [src/y.md]\nbroad:")


# ValidateTest를 상속하지 않는다 — 바꾼 registry·케이스로 validate 테스트가 다시 돌면 깨진다
class RunBSelectTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add

    def setUp(self):
        ValidateTest.setUp(self)
        self.write(".claude/regress/registry.yaml", B_REGISTRY)
        self.add("b-two", case_text(B_FM, B_BODY, id="b-two", components="[y]"))
        self.add("b-one", case_text(B_FM, B_BODY, id="b-one", components="[x]"))
        self.add("a-pos", case_text(A_FM, A_BODY, components="[x]"))

    def b(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main_b(list(args), regress=self.regress, root=self.root)
        return code, out.getvalue().splitlines(), err.getvalue()

    def assertNoSummary(self, lines):
        self.assertFalse([x for x in lines if x.startswith("B층 선택:")], lines)

    # 고른 케이스 줄(`<id> ← <이유>`)과 요약 줄 N/M·K가 단계 6과 사람이 읽는 형식 — A층 케이스는 M에 들지 않는다
    def test_b_select_line_and_summary(self):
        code, lines, _ = self.b("--select", "--files", "src/x/a.md")
        self.assertTrue(any(x.startswith("b-one ← ") for x in lines), lines)
        self.assertEqual(lines[-1], "B층 선택: 1/2 · 바뀐 파일 1")
        self.assertEqual(code, 0)

    # 출력 순서가 입력·파일 순서에 따라 흔들리면 비교·기록이 어긋난다 — 케이스 id 순
    def test_b_select_sorted_by_id(self):
        code, lines, _ = self.b("--select", "--files", "src/y.md", "src/x/a.md")
        picked = [x.split(" ← ")[0] for x in lines if " ← " in x]
        self.assertEqual(picked, ["b-one", "b-two"])
        self.assertEqual(lines[-1], "B층 선택: 2/2 · 바뀐 파일 2")
        self.assertEqual(code, 0)

    # 고를 것이 없는 것도 정상 결과 — 요약 줄만 내고 종료 코드 0 (2로 끝나면 hook·스크립트가 오류로 읽는다)
    def test_b_select_zero_exit0(self):
        code, lines, _ = self.b("--select", "--files", "wiki/index.md")
        self.assertEqual(lines, ["B층 선택: 0/2 · 바뀐 파일 1"])
        self.assertEqual(code, 0)

    # 잘못 부른 것을 "0개 선택"으로 보이면 안 된다 — stderr 이유 + 종료 코드 2, 요약 줄 없음
    def test_b_usage_errors_exit2(self):
        for args in ([], ["--select", "--x"], ["--select", "--since"], ["--select", "--root"],
                     ["--select", "--files", "a", "--since", "HEAD"]):
            with self.subTest(args=args):
                code, lines, err = self.b(*args)
                self.assertEqual(code, 2)
                self.assertTrue(err.strip(), args)
                self.assertNoSummary(lines)

    # git을 못 읽었는데 "바뀐 파일 0"으로 보이면 회귀를 놓친다 — git 저장소가 아닌 --root는 stderr 이유 + 2
    def test_b_git_failure_exit2(self):
        nogit = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, nogit)
        code, lines, err = self.b("--select", "--root", nogit)
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())
        self.assertNoSummary(lines)

    # 이유 `<component> (<파일>)`는 사람이 왜 골랐는지 보는 근거 — 줄 머리만 맞고 이유가 빠지거나 바뀌면 FAIL
    def test_b_component_reason_format(self):
        code, lines, _ = self.b("--select", "--files", "src/x/a.md")
        self.assertIn("b-one ← x (src/x/a.md)", lines)
        self.assertEqual(code, 0)

    # 지운 파일·rename의 옛 경로도 바뀐 것이다 — 파일 시스템으로 맞추면 없는 경로를 놓친다. `**`는 여러 단계 아래까지
    def test_b_glob_deep_and_missing_path(self):
        self.assertFalse(os.path.exists(os.path.join(self.root, "src", "x", "d", "e", "gone.md")))
        code, lines, _ = self.b("--select", "--files", "src/x/d/e/gone.md")
        self.assertIn("b-one ← x (src/x/d/e/gone.md)", lines)
        self.assertEqual(lines[-1], "B층 선택: 1/2 · 바뀐 파일 1")
        self.assertEqual(code, 0)

    # 접두사만 같은 경로·폴더 경로 자체·`*`가 `/`를 넘는 경로·끝이 더 붙은 경로를 고르면 관련 없는 케이스까지 돈다
    def test_b_glob_no_prefix_match(self):
        self.write(".claude/regress/registry.yaml", B_REGISTRY.replace("broad:", "  z: [src/*.md]\nbroad:"))
        self.add("b-zed", case_text(B_FM, B_BODY, id="b-zed", components="[z]"))
        for files in (["src/xx/a.md", "src/x"], ["src/xa/b.md"], ["src/q.md.bak"]):
            with self.subTest(files=files):
                code, lines, _ = self.b("--select", "--files", *files)
                self.assertEqual(lines, [f"B층 선택: 0/3 · 바뀐 파일 {len(files)}"])
                self.assertEqual(code, 0)
        code, lines, _ = self.b("--select", "--files", "src/q.md")
        self.assertEqual(lines, ["b-zed ← z (src/q.md)", "B층 선택: 1/3 · 바뀐 파일 1"])
        self.assertEqual(code, 0)

    # broad component(리뷰어·schema)는 모든 B층 판정에 영향을 준다 — 케이스 components에 없어도 B층 전부, A층은 빼고
    def test_b_select_broad_all(self):
        code, lines, _ = self.b("--select", "--files", "CLAUDE.md")
        self.assertEqual(lines, ["b-one ← broad schema (CLAUDE.md)", "b-two ← broad schema (CLAUDE.md)",
                                 "B층 선택: 2/2 · 바뀐 파일 1"])
        self.assertEqual(code, 0)

    # 이유에 broad가 보여야 '전체 실행'인 까닭을 안다 — 케이스 components에도 있는 broad는 이유가 겹쳐 붙지 않는다
    def test_b_select_broad_no_dup_reason(self):
        self.add("b-three", case_text(B_FM, B_BODY, id="b-three", components="[wiki-reviewer]"))
        code, lines, _ = self.b("--select", "--files", ".claude/agents/wiki-reviewer.md")
        self.assertIn("b-three ← broad wiki-reviewer (.claude/agents/wiki-reviewer.md)", lines)
        self.assertEqual(lines[-1], "B층 선택: 3/3 · 바뀐 파일 1")
        self.assertEqual(code, 0)

    # 위키 내용·원본 자료 변경은 시스템 동작을 바꾸지 않는다 — B층(LLM 비용)을 돌리지 않는다
    def test_b_unrelated_zero(self):
        code, lines, _ = self.b("--select", "--files", "wiki/index.md", "raw/x.md")
        self.assertEqual(lines, ["B층 선택: 0/2 · 바뀐 파일 2"])
        self.assertEqual(code, 0)

    # A층은 `run.py A`로 전부 돈다 — B층 선택에 끼면 단계 6이 A층 케이스를 리뷰어로 돌린다
    # (component·broad·`files`·케이스 파일 어느 경로로도 — A층 `files`·A층 케이스 파일도 바뀐 것으로 준다)
    def test_b_a_layer_never(self):
        os.makedirs(os.path.join(self.root, "src"))
        self.write("src/a-only.md", "x")
        self.add("a-files", case_text(A_FM, A_BODY, id="a-files", components="[x]", files="[src/a-only.md]"))
        code, lines, _ = self.b("--select", "--files", "src/x/a.md", "src/a-only.md",
                                ".claude/regress/cases/a-pos.md", ".claude/regress/cases/a-files.md", "CLAUDE.md")
        picked = [x.split(" ← ")[0] for x in lines if " ← " in x]
        self.assertEqual(picked, ["b-one", "b-two"])
        self.assertFalse([x for x in lines if x.startswith("a-")], lines)
        self.assertEqual(lines[-1], "B층 선택: 2/2 · 바뀐 파일 5")
        self.assertEqual(code, 0)

    def add_fx(self):
        os.makedirs(os.path.join(self.root, "src"))
        self.write("src/fx.md", "x")
        self.add("b-fx", case_text(B_FM, B_BODY, id="b-fx", components="[y]", files="[src/fx.md]"))

    # 케이스가 직접 기대는 파일이 바뀌면 component glob과 무관하게 판정이 바뀐다 — `files`만으로도 고른다
    def test_b_select_case_files(self):
        self.add_fx()
        code, lines, _ = self.b("--select", "--files", "src/fx.md")
        self.assertEqual(lines, ["b-fx ← files (src/fx.md)", "B층 선택: 1/3 · 바뀐 파일 1"])
        self.assertEqual(code, 0)

    # 케이스 파일 자체(기대·미니 볼트)를 고치면 그 케이스를 다시 돌려야 한다 — 다른 케이스는 고르지 않는다
    def test_b_select_case_file_itself(self):
        code, lines, _ = self.b("--select", "--files", ".claude/regress/cases/b-two.md")
        self.assertEqual(lines, ["b-two ← 케이스 파일", "B층 선택: 1/2 · 바뀐 파일 1"])
        self.assertEqual(code, 0)

    # 이유가 다 보여야 사람이 왜 골랐는지 안다 — component·files·케이스 파일 이유가 한 줄에 모두 붙는다
    def test_b_select_reasons_all(self):
        self.add_fx()
        code, lines, _ = self.b("--select", "--files", "src/y.md", "src/fx.md", ".claude/regress/cases/b-fx.md")
        self.assertIn("b-fx ← y (src/y.md); files (src/fx.md); 케이스 파일", lines)
        self.assertIn("b-two ← y (src/y.md)", lines)
        self.assertEqual(lines[-1], "B층 선택: 2/3 · 바뀐 파일 3")
        self.assertEqual(code, 0)

    def assertRejected(self, code, lines, err):
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())
        self.assertNoSummary(lines)
        self.assertFalse([x for x in lines if " ← " in x], lines)

    # 형식이 틀린 케이스가 선택에서 조용히 빠지면 회귀를 놓친 채 "N/M"이 정상처럼 보인다
    # — registry에 없는 component는 고르지 않고 오류 줄을 stderr에 + 2 (broad 입력이라 검사가 없으면 b-bad까지 고른다)
    def test_b_validate_error_exit2(self):
        self.add("b-bad", case_text(B_FM, B_BODY, id="b-bad", components="[nope]"))
        code, lines, err = self.b("--select", "--files", "CLAUDE.md")
        self.assertRejected(code, lines, err)
        self.assertIn("component가 registry에 없음: nope", err)

    # frontmatter·본문이 깨져 읽히지 않는 B층 케이스도 선택·요약 M에서 조용히 빠지면 안 된다
    def test_b_validate_parse_error_exit2(self):
        self.add("b-broken", case_text(B_FM, B_BODY.replace("[3]", "[3"), id="b-broken", components="[x]"))
        code, lines, err = self.b("--select", "--files", "src/x/a.md")
        self.assertRejected(code, lines, err)
        self.assertIn("b-broken", err)

    # 경고(맞는 파일 없는 glob — 이 가짜 볼트의 x·y)로는 막지 않는다 — 막으면 정상 선택까지 멈춘다
    def test_b_validate_warning_not_rejected(self):
        self.assertTrue(run.validate(self.regress, self.root)[1])
        self.assertFalse(run.validate(self.regress, self.root)[0])
        code, lines, _ = self.b("--select", "--files", "src/x/a.md")
        self.assertEqual(lines[-1], "B층 선택: 1/2 · 바뀐 파일 1")
        self.assertEqual(code, 0)

    # `--files` 없이 git 입력이 선택·요약 줄까지 이어져야 hook·단계 6이 그대로 쓴다 — 추적 안 되는 새 파일도 바뀐 것
    def test_b_git_input_select(self):
        git_in(self.root, "init", "-q")
        git_in(self.root, "add", "-A")
        git_in(self.root, "commit", "-q", "-m", "base")
        os.makedirs(os.path.join(self.root, "src", "x"))
        self.write("src/x/a.md", "x")
        code, lines, _ = self.b("--select")
        self.assertIn("b-one ← x (src/x/a.md)", lines)
        self.assertEqual(lines[-1], "B층 선택: 1/2 · 바뀐 파일 1")
        self.assertEqual(code, 0)

    # 루프 verify·hook이 실제 볼트에서 --select를 몇 번이고 돌린다 — 영수증·위키·원본·git·history 어느 것도 바뀌면 안 된다
    # (history "선택됨" 기록은 단계 6 --prepare 몫. .git/은 git diff가 index를 다시 쓸 수 있어 바이트 비교에서 빼고 porcelain으로 본다)
    def test_b_select_read_only(self):
        for d in ("wiki", "raw", "src/x"):
            os.makedirs(os.path.join(self.root, d))
        for p, t in (("wiki/index.md", "root-wiki"), ("raw/2026-10-01_x.md", "root-raw"),
                     (".claude/.lint_state.json", '{"keep": 1}')):
            self.write(p, t)
        git_in(self.root, "init", "-q")
        git_in(self.root, "add", "-A")
        git_in(self.root, "commit", "-q", "-m", "base")
        self.write("src/x/a.md", "x")                  # 커밋 안 된 변경: 추적 안 되는 새 파일 + 수정한 위키
        self.write("wiki/index.md", "root-wiki-2")

        def tree():
            out = {}
            for d, ds, ns in os.walk(self.root):
                ds[:] = [x for x in ds if x != ".git"]
                for n in ns:
                    with open(os.path.join(d, n), "rb") as f:
                        out[os.path.relpath(os.path.join(d, n), self.root)] = f.read()
            return out

        def porcelain():
            return git_in(self.root, "status", "--porcelain", "-z")

        before, status = tree(), porcelain()
        for args in (["--select"], ["--select", "--since", "HEAD"], ["--select", "--files", "CLAUDE.md", "src/x/a.md"]):
            with self.subTest(args=args):
                code, lines, _ = self.b(*args)
                self.assertTrue(lines and lines[-1].startswith("B층 선택: "), lines)
                self.assertEqual(code, 0)
                self.assertEqual(tree(), before)
                self.assertEqual(porcelain(), status)
                self.assertFalse(os.path.exists(os.path.join(self.regress, "history.jsonl")))
                self.assertFalse(os.path.exists(os.path.join(self.root, "history.jsonl")))


# RunBSelectTest를 상속하지 않는다 — 상속하면 --select 테스트가 이 클래스에서 한 번 더 돈다
class RunBModeTest(unittest.TestCase):
    setUp, tearDown, write, add = RunBSelectTest.setUp, ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b, assertNoSummary = RunBSelectTest.b, RunBSelectTest.assertNoSummary

    # 모드를 잘못 부른 것이 일부만 돌거나 "0개"로 보이면 안 된다 — 이유가 서로 구분되는 stderr + B_USAGE + 2,
    # 요약 줄 없음, 실행 폴더(--base)에 아무것도 생기지 않음
    def test_b_mode_usage_errors_exit2(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base)
        judge = ["--judge", "r1", "v1", "o", "t"]
        for args, why in (([], "모드"),
                          (["--select", "--prepare", "r1"], "하나만"),
                          (["--prepare", "r1", "--judge", "r1", "v1", "o", "t"], "하나만"),
                          (["--prepare"], "run-id가 없음"),          # B_USAGE에도 "run-id"가 있어 낱말만으로는 늘 통과
                          (["--prepare", "--files", "a"], "run-id가 없음"),
                          (["--judge", "r1", "v1", "o"], "4개"),
                          (["--judge", "r1", "v1", "o", "t", "x"], "4개"),
                          (["--prepare", "r1", "--base"], "값이 없음"),
                          (judge + ["--detect"], "값이 없음"),
                          (judge + ["--not-detect"], "값이 없음"),
                          (judge + ["--detect", "아마"], "허용"),
                          (judge + ["--not-detect", "예"], "허용"),
                          (["--prepare", "r1", "--files", "a", "--since", "HEAD"], "함께"),
                          (["--prepare", "r1", "--x"], "모르는 옵션")):
            with self.subTest(args=args):
                code, lines, err = self.b(*args, "--base", base)
                self.assertEqual(code, 2)
                self.assertIn(why, err)
                self.assertIn(run.B_USAGE, err)
                self.assertNoSummary(lines)
                self.assertEqual(os.listdir(base), [])


# 변형 둘은 파일 안 순서(z-앞 → a-뒤)가 이름 순과 반대 — 변형을 정렬해 버리면 순서 테스트가 깨진다
# (한글 "나중"·"먼저"는 이름 순이 파일 순과 같아 정렬 변이를 못 잡는다)
B_VARIANTS = """
## 변형: z-앞
### 파일: wiki/요약-원문.md
~~~~
앞
~~~~

## 변형: a-뒤
### 파일: wiki/요약-원문.md
~~~~
뒤
~~~~
"""


# RunBSelectTest를 상속하지 않는다 — 상속하면 --select 테스트가 바뀐 b-one으로 한 번 더 돈다
class RunBPrepareUnitsTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b, assertNoSummary = RunBSelectTest.b, RunBSelectTest.assertNoSummary

    def setUp(self):
        RunBSelectTest.setUp(self)
        self.add("b-one", case_text(B_FM, B_BODY + B_VARIANTS, id="b-one", components="[x]"))
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base)

    # 리뷰어 호출 1회 = 유닛 1개 — 고른 케이스마다 본 케이스 + 변형 전부(케이스 id 순, 변형은 파일 안 순서), 이유는 선택 이유 그대로
    def test_b_prepare_units_from_select(self):
        changed = ["src/y.md", "src/x/a.md"]
        units = run.b_units(changed, None, self.regress, self.root)
        self.assertEqual([u["unit"] for u in units], ["b-one", "b-one--z-앞", "b-one--a-뒤", "b-two"])
        self.assertEqual([(u["case"], u["variant"]) for u in units],
                         [("b-one", None), ("b-one", "z-앞"), ("b-one", "a-뒤"), ("b-two", None)])
        why = dict(run.select_b(changed, self.regress, self.root))
        for u in units:
            self.assertEqual(u["reasons"], why[u["case"]], u)

    # 사람이 고른 유닛은 그것만 돈다 — 변형을 덧붙이거나 순서를 바꾸면 지정한 실행이 아니다. 이유는 "지정"
    def test_b_prepare_units_explicit(self):
        units = run.b_units(None, ["b-two", "b-one--a-뒤"], self.regress, self.root)
        self.assertEqual([(u["unit"], u["case"], u["variant"], u["reasons"]) for u in units],
                         [("b-two", "b-two", None, ["지정"]), ("b-one--a-뒤", "b-one", "a-뒤", ["지정"])])

    # 잘못 적은 유닛이 하나라도 있으면 일부만 준비하지 않는다 — 이유가 구분되는 stderr + 2, 실행 폴더 없음
    def test_b_prepare_bad_unit_exit2(self):
        for units, why in ((["없는-케이스"], "없는 케이스"), (["b-one--없는변형"], "없는 변형"),
                           (["a-pos"], "B층"), (["b-two", "없는-케이스"], "없는 케이스")):
            with self.subTest(units=units):
                code, lines, err = self.b("--prepare", "r1", *units, "--base", self.base)
                self.assertEqual(code, 2)
                self.assertIn(why, err)
                self.assertNoSummary(lines)
                self.assertEqual(os.listdir(self.base), [])

    # 형식 오류 케이스가 유닛에서 조용히 빠지면 안 된다 — --select와 같이 validate 오류면 2 (유닛을 직접 줘도)
    def test_b_prepare_validate_error_exit2(self):
        self.add("b-broken", case_text(B_FM, B_BODY.replace("[3]", "[3"), id="b-broken", components="[x]"))
        for args in (["--files", "src/x/a.md"], ["b-two"]):
            with self.subTest(args=args):
                code, lines, err = self.b("--prepare", "r1", *args, "--base", self.base)
                self.assertEqual(code, 2)
                self.assertIn("validate", err)
                self.assertIn("b-broken", err)
                self.assertEqual(os.listdir(self.base), [])

    # 고를 것이 없는 것도 정상 결과 — 빈 실행 폴더 없이 요약 줄 + 0 (2로 끝나면 세션이 오류로 읽는다)
    def test_b_prepare_zero_units_exit0(self):
        code, lines, err = self.b("--prepare", "r1", "--files", "wiki/index.md", "--base", self.base)
        self.assertEqual(lines, ["B층 준비: 유닛 0 · 케이스 0/2 · 바뀐 파일 1"], err)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.base, "r1")))


# RunBPrepareUnitsTest를 상속하지 않는다 — 상속하면 유닛 테스트가 바뀐 루트(rules·settings.json)로 한 번 더 돈다
class RunBPrepareVaultTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b = RunBSelectTest.b

    def setUp(self):
        RunBPrepareUnitsTest.setUp(self)
        # ValidateTest의 가짜 루트에는 .claude/rules/가 없다 — 없으면 copytree가 OSError로 끝나 기준 실패처럼 보인다
        os.makedirs(os.path.join(self.root, ".claude", "rules"))
        self.write(".claude/rules/페이지-형식.md", "# 형식\n")
        self.write(".claude/settings.json", "{}")    # 루트에 있는데 볼트에 안 넣는 것을 보려고
        self.run_dir = os.path.join(self.base, "r1")

    def prepare(self, *args):
        return self.b("--prepare", *args, "--base", self.base)

    def tree(self, top):
        return {os.path.relpath(os.path.join(d, n), top).replace(os.sep, "/")
                for d, _, names in os.walk(top) for n in names}

    # 케이스 id·변형 이름이 경로로 리뷰어에게 보이면 실패 유형이 드러난다 — 폴더 이름은 불투명한 번호뿐
    def test_b_prepare_vault_names_opaque(self):
        code, _, err = self.prepare("r1", "b-two", "b-one--a-뒤")
        self.assertEqual(code, 0, err)
        dirs = sorted(n for n in os.listdir(self.run_dir) if os.path.isdir(os.path.join(self.run_dir, n)))
        self.assertEqual(dirs, ["v1", "v2"])     # 폴더만 본다 — 실행 폴더에 다른 파일(요청문 등)이 생겨도 깨지지 않게
        for d, subdirs, names in os.walk(self.run_dir):
            for n in subdirs + names:
                rel = os.path.relpath(os.path.join(d, n), self.run_dir)
                for word in ("b-two", "b-one", "a-뒤"):
                    self.assertNotIn(word, rel)

    # 리뷰어는 현재 시스템 파일과 케이스 파일만 본다 — regress·settings.json(hook)·wiki_lint.py가 들어가면 세션 방식이 아니다
    def test_b_prepare_vault_contents(self):
        code, _, err = self.prepare("r1", "b-two", "b-one--a-뒤")
        self.assertEqual(code, 0, err)
        v1, v2 = os.path.join(self.run_dir, "v1"), os.path.join(self.run_dir, "v2")
        system = {"CLAUDE.md", ".claude/agents/wiki-reviewer.md", ".claude/rules/페이지-형식.md"}
        self.assertEqual(self.tree(v1), system | {"raw/2026-10-01_원문.md", "wiki/요약-원문.md"})
        for rel in system:
            with open(os.path.join(self.root, rel), "rb") as a, open(os.path.join(v1, rel), "rb") as b:
                self.assertEqual(a.read(), b.read(), rel)
        self.assertEqual(run.read(os.path.join(v2, "wiki", "요약-원문.md")), "뒤\n")     # 변형이 덮어씀
        self.assertEqual(run.read(os.path.join(v2, "raw", "2026-10-01_원문.md")),
                         run.read(os.path.join(v1, "raw", "2026-10-01_원문.md")))      # 변형이 안 적은 칸은 본 케이스 것
        self.assertIn("pin versions", run.read(os.path.join(v2, "raw", "2026-10-01_원문.md")))

    # 볼트 이름이 불투명하니 유닛·회차는 manifest로만 되찾는다 — --judge가 이것을 읽는다
    def test_b_prepare_manifest(self):
        code, _, err = self.prepare("r1", "b-two", "b-one--a-뒤")
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.run_dir, "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        rest = {"round": 1, "attempt": 1, "result": None}
        self.assertEqual(m, {"v1": {"unit": "b-two", "case": "b-two", "variant": None, **rest},
                             "v2": {"unit": "b-one--a-뒤", "case": "b-one", "variant": "a-뒤", **rest}})
        # 선택 경로도 준비 순서 = b_units 순서 (변형은 파일 안 순서 z-앞 → a-뒤)
        code, _, err = self.prepare("r2", "--files", "src/x/a.md")
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.base, "r2", "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual([(k, v["unit"]) for k, v in m.items()],
                         [("v1", "b-one"), ("v2", "b-one--z-앞"), ("v3", "b-one--a-뒤")])

    # 실행마다 새로 — 이미 있는 실행 폴더에 섞어 넣으면 앞 실행의 볼트·manifest와 번호가 엉킨다
    def test_b_prepare_existing_run_exit2(self):
        os.makedirs(self.run_dir)
        code, _, err = self.prepare("r1", "b-two")
        self.assertEqual(code, 2)
        self.assertIn("이미 있음", err)          # 우연한 2(아직 구현되지 않음 등)와 구분
        self.assertEqual(os.listdir(self.run_dir), [])


# 본 케이스에 raw 둘·비교 대상 wiki 페이지, 변형 z-앞에 수정한 페이지 칸(파일 안 순서 요약 → 비교, 경로 순과 반대)과 변형만의 raw
B_REQUEST_BODY = B_BODY + """
## 파일: raw/2026-10-02_둘째.md
~~~~
둘째
~~~~

## 파일: wiki/비교-페이지.md
~~~~
비교
~~~~

## 변형: z-앞
### 수정한 페이지
- wiki/요약-원문.md
- wiki/비교-페이지.md

### 파일: raw/2026-10-03_변형.md
~~~~
변형
~~~~

## 변형: a-뒤
### 파일: wiki/요약-원문.md
~~~~
뒤
~~~~
"""


# RunBPrepareVaultTest를 상속하지 않는다 — 상속하면 볼트 테스트가 바뀐 b-one·규칙 파일로 한 번 더 돈다
class RunBPrepareRequestTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b = RunBSelectTest.b
    prepare, tree = RunBPrepareVaultTest.prepare, RunBPrepareVaultTest.tree

    def setUp(self):
        RunBPrepareVaultTest.setUp(self)
        self.write(".claude/rules/a-기타.md", "# 기타\n")     # 규칙 파일 둘 — 순서와 `, ` 잇기를 본다
        self.add("b-one", case_text(B_FM, B_REQUEST_BODY, id="b-one", components="[x]"))

    def request(self, k, run_id="r1"):
        return run.read(os.path.join(self.base, run_id, f"v{k}.request.md"))

    # 세션은 요청문을 그대로 리뷰어에 넘긴다 — 줄 모양이 spec과 한 글자라도 다르면 리뷰어 입력이 실행마다 흔들린다
    # 규칙 줄은 spec 예시대로 CLAUDE.md 먼저, 그 뒤 .claude/rules/ 경로 순 (문자열 정렬이면 .claude가 앞)
    def test_b_prepare_request_exact(self):
        code, _, err = self.prepare("r1", "b-one", "b-one--a-뒤")
        self.assertEqual(code, 0, err)
        V = os.path.join(os.path.abspath(self.base), "r1", "v1")
        self.assertEqual(self.request(1),
                         "자료 넣기 작업을 검토해 줘.\n\n"
                         f"볼트 위치: {V}/ (검토 범위: 이 폴더. 경로는 모두 이 폴더 기준 절대 경로로 읽는다)\n"
                         f"- 규칙: {V}/CLAUDE.md, {V}/.claude/rules/a-기타.md, {V}/.claude/rules/페이지-형식.md\n"
                         f"- 수정한 페이지: {V}/wiki/요약-원문.md\n"
                         f"- 관련 raw: {V}/raw/2026-10-01_원문.md, {V}/raw/2026-10-02_둘째.md\n")
        self.assertNotIn("비교-페이지", self.request(1))       # 비교 대상은 리뷰어가 스스로 찾을 몫
        V2 = os.path.join(os.path.abspath(self.base), "r1", "v2")
        pages = [x for x in self.request(2).splitlines() if x.startswith("- 수정한 페이지:")]
        self.assertEqual(pages, [f"- 수정한 페이지: {V2}/wiki/요약-원문.md"])     # 변형에 칸이 없으면 본 케이스 것

    # 변형이 수정한 페이지를 바꾸면 그것을 넘긴다(경로 순). 관련 raw는 덮어쓰기 뒤 볼트 raw/ 전부
    def test_b_prepare_request_variant_pages(self):
        code, _, err = self.prepare("r1", "b-one--z-앞")
        self.assertEqual(code, 0, err)
        V = os.path.join(os.path.abspath(self.base), "r1", "v1")
        lines = self.request(1).splitlines()
        self.assertIn(f"- 수정한 페이지: {V}/wiki/비교-페이지.md, {V}/wiki/요약-원문.md", lines)
        self.assertIn(f"- 관련 raw: {V}/raw/2026-10-01_원문.md, {V}/raw/2026-10-02_둘째.md, "
                      f"{V}/raw/2026-10-03_변형.md", lines)

    # 요청문은 볼트 밖 실행 폴더 바로 아래 — 볼트 안에 두면 리뷰어가 볼 것이 아닌 파일이 섞인다
    def test_b_prepare_request_outside_vault(self):
        code, _, err = self.prepare("r1", "b-two", "b-one--a-뒤")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(n for n in os.listdir(self.run_dir) if n.endswith(".request.md")),
                         ["v1.request.md", "v2.request.md"])
        for k in (1, 2):
            self.assertFalse([p for p in self.tree(os.path.join(self.run_dir, f"v{k}")) if p.endswith(".request.md")])

    # 요청문은 "이 폴더 기준 절대 경로"라고 말한다 — 상대 --base에서 경로가 상대로 남으면 리뷰어가 다른 곳 기준으로 풀 수 있다
    def test_b_prepare_request_relative_base(self):
        code, _, err = self.b("--prepare", "r1", "b-two", "--base", os.path.relpath(self.base))
        self.assertEqual(code, 0, err)
        where = [x for x in self.request(1).splitlines() if x.startswith("볼트 위치: ")]
        self.assertEqual(len(where), 1)
        path = where[0][len("볼트 위치: "):].split(" ")[0]
        self.assertTrue(os.path.isabs(path), path)
        self.assertEqual(path, os.path.join(os.path.abspath(self.base), "r1", "v1") + "/")


# 금지 낱말을 일부러 케이스 id·변형 이름·기대 문장에 넣는다 — 파일 이름(요약-원문 등)에는 넣지 않는다
B_BLIND_BODY = B_BODY.replace("- 권고 강도가 원문보다 약화됨", "- Failure 기대 문장 MUST_보임") \
                     .replace("- 출처 간 상충", "- regress 테스트 문장") + """
## 변형: TEST-기대-failure
### 기대
reviewer_items: [3]
must_detect:
- must_ 변형 기대 문장

### 파일: wiki/요약-원문.md
~~~~
뒤
~~~~
"""


# RunBPrepareVaultTest를 상속하지 않는다 — 상속하면 볼트 테스트가 바뀐 케이스 목록으로 한 번 더 돈다
class RunBPrepareBlindTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b = RunBSelectTest.b
    prepare, tree = RunBPrepareVaultTest.prepare, RunBPrepareVaultTest.tree
    request = RunBPrepareRequestTest.request
    CID, VAR = "test-회귀-케이스", "TEST-기대-failure"

    def setUp(self):
        RunBPrepareVaultTest.setUp(self)
        self.add(self.CID, case_text(B_FM, B_BLIND_BODY, id=self.CID, components="[x]"))

    # 요청문에 케이스 id·변형 이름·failure_class·기대 문장·검사용 낱말이 보이면 리뷰어가 무엇을 찾아야 하는지 안다
    def test_b_prepare_request_blind(self):
        code, _, err = self.prepare("r1", self.CID, f"{self.CID}--{self.VAR}")
        self.assertEqual(code, 0, err)
        run_dir = os.path.join(os.path.abspath(self.base), "r1") + "/"
        banned = ["기대", "failure", "must_", "test", "테스트", "회귀", "regress", "케이스",
                  self.CID, self.VAR, "recommendation-strength-loss",
                  "Failure 기대 문장 MUST_보임", "regress 테스트 문장", "must_ 변형 기대 문장"]
        for k in (1, 2):
            raw = self.request(k)
            self.assertIn(run_dir, raw)          # 지우기가 헛돌면 아래 검사가 빈 문자열을 보게 된다
            low = raw.replace(run_dir, "").lower()
            for w in banned:
                with self.subTest(v=k, word=w):
                    self.assertNotIn(w.lower(), low)

    # 케이스가 정한 파일 이름은 요청문에 그대로 나온다 — 이름에 금지 낱말이 있으면 요청문을 쓰지 않고 2, 실행 폴더도 남기지 않는다
    def test_b_prepare_request_blind_filename(self):
        for cid, body, pages, word in (
                ("b-raw", B_BODY.replace("2026-10-01_원문", "2026-10-01_test-원문"), "[wiki/요약-원문.md]", "test"),
                ("b-page", B_BODY.replace("요약-원문", "요약-회귀-원문"), "[wiki/요약-회귀-원문.md]", "회귀")):
            with self.subTest(case=cid):
                self.add(cid, case_text(B_FM, body, id=cid, components="[x]", **{"수정한 페이지": pages}))
                code, _, err = self.prepare(cid, cid)
                self.assertEqual(code, 2, err)
                self.assertIn(f"블라인드 위반: {cid} — {word}", err)
                self.assertFalse(os.path.exists(os.path.join(self.base, cid)))


# RunBPrepareVaultTest를 상속하지 않는다 — 상속하면 볼트 테스트가 한 번 더 돈다. history는 가짜 regress 폴더(self.regress)에만 쓴다
class RunBPrepareHistoryTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b = RunBSelectTest.b
    prepare = RunBPrepareVaultTest.prepare

    def setUp(self):
        RunBPrepareVaultTest.setUp(self)
        self.hist = os.path.join(self.regress, "history.jsonl")

    def rows(self):
        with open(self.hist, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    # 어느 실행에서 어떤 유닛을 왜 골랐는지는 history로만 남는다 — 칸이 빠지거나 더해지면 뒤 판정 기록과 못 잇는다
    def test_b_prepare_history_explicit(self):
        code, _, err = self.prepare("r1", "b-two", "b-one--a-뒤")
        self.assertEqual(code, 0, err)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertRegex(r["date"], r"^\d{4}-\d{2}-\d{2}$")
        today = datetime.date.today().isoformat()
        self.assertEqual(rows, [
            {"event": "선택됨", "date": today, "run_id": "r1", "vault": "v1", "unit": "b-two",
             "case": "b-two", "variant": None, "reasons": ["지정"]},
            {"event": "선택됨", "date": today, "run_id": "r1", "vault": "v2", "unit": "b-one--a-뒤",
             "case": "b-one", "variant": "a-뒤", "reasons": ["지정"]}])

    # 선택 경로는 select_b의 이유를 그대로 남긴다 — 왜 돌았는지(어느 바뀐 파일 때문인지) 나중에 되짚게
    def test_b_prepare_history_selected(self):
        code, _, err = self.prepare("r1", "--files", "src/x/a.md")
        self.assertEqual(code, 0, err)
        why = dict(run.select_b(["src/x/a.md"], self.regress, self.root))["b-one"]
        self.assertEqual([(r["vault"], r["unit"], r["reasons"]) for r in self.rows()],
                         [("v1", "b-one", why), ("v2", "b-one--z-앞", why), ("v3", "b-one--a-뒤", why)])

    # history는 쌓이는 기록 — 새 실행이 앞 실행 줄을 덮어쓰면 이력이 사라진다
    def test_b_prepare_history_appends(self):
        self.assertEqual(self.prepare("r1", "b-two")[0], 0)
        first = self.rows()
        self.assertEqual(self.prepare("r2", "b-one")[0], 0)
        rows = self.rows()
        self.assertEqual(rows[:1], first)
        self.assertEqual([(r["run_id"], r["unit"]) for r in rows], [("r1", "b-two"), ("r2", "b-one")])

    # 준비가 아무것도 만들지 않고 끝나면 "선택됨"도 남기지 않는다 — 돌지 않은 유닛이 이력에 섞이면 판정과 안 맞는다
    def test_b_prepare_history_untouched_on_fail(self):
        def existing():
            os.makedirs(self.run_dir)
            return self.prepare("r1", "b-two")[0]

        def blind():
            self.add("b-raw", case_text(B_FM, B_BODY.replace("2026-10-01_원문", "2026-10-01_test-원문"),
                                        id="b-raw", components="[x]"))
            return self.prepare("r-blind", "b-raw")[0]

        for label, go, want in (("잘못된 유닛", lambda: self.prepare("r-bad", "없는-케이스")[0], 2),
                                ("이미 있는 실행 폴더", existing, 2),
                                ("유닛 0개", lambda: self.prepare("r-zero", "--files", "wiki/index.md")[0], 0),
                                ("블라인드 위반", blind, 2)):
            with self.subTest(label):
                if os.path.exists(self.hist):    # 앞 subTest가 남긴 줄로 뒤 subTest가 따라 깨지지 않게 (변이 때 어느 경로가 잡았는지 가린다)
                    os.remove(self.hist)
                self.assertEqual(go(), want)
                self.assertFalse(os.path.exists(self.hist))


REVIEW_OUT = """\
의미 검토 — 1:✅ 2:⚠️1건 3:⚠️1건 4:✅ 5:✅ 6:✅ 7:✅ 8:✅
문제:
- [3] /v/wiki/x.md:5 — 권고 강도 약화 — "must pin" — 원문대로
- [2] /v/wiki/y.md:2 — 수치 다름 — "3개" — 고친다
REVIEW-RESULT: issues=2
"""

B_VARIANT_BODY = B_BODY.replace("reviewer_items: [3]", "reviewer_items: [5]") + """
## 변형: 음성
### 기대
reviewer_items: []

## 변형: 기대없음
### 파일: wiki/요약-원문.md
~~~~
x
~~~~
"""


# 결정적 대조는 CLI(--judge)가 누출·의미 대조·회차에 기대므로 일 함수만 직접 본다 — 가짜 볼트가 필요 없다
class RunBJudgeItemsTest(unittest.TestCase):
    # 리뷰어 출력에서 줄머리 문제 줄 번호와 REVIEW-RESULT만 세야 한다 — 본문 속 `- [N]`·자리표시를 세면 항목이 맞은 것처럼 보인다
    def test_b_review_parse(self):
        self.assertEqual(run.parse_review(REVIEW_OUT), ({2, 3}, 2))
        noisy = REVIEW_OUT.replace("문제:\n", "문제:\n- 본문 속 - [7] 언급\n  - [8] 들여씀\n- [항목번호] 자리표시\n- [3a] 정수 아님\n")
        self.assertEqual(run.parse_review(noisy), ({2, 3}, 2))
        self.assertEqual(run.parse_review("의미 검토 — 1:✅\n문제: 없음\nREVIEW-RESULT: issues=0\n"), (set(), 0))
        self.assertEqual(run.parse_review(REVIEW_OUT.replace("REVIEW-RESULT: issues=2\n", "")), ({2, 3}, None))

    # 참고 절의 줄머리 `- [N]`은 문제가 아니다 — b-full-2에서 issues=0 출력이 참고의 [3]·[4]·[5]로 항목을 맞춘 것처럼 보였다
    def test_b_review_parse_problem_section_only(self):
        tail = "참고:\n- [5] 상충 대상 없음\n- [8] 들어오는 링크 확인\n확인한 내용 (문제 없음):\n- [1] 출처 있음\n"
        self.assertEqual(run.parse_review(REVIEW_OUT.replace("REVIEW-RESULT", tail + "REVIEW-RESULT")), ({2, 3}, 2))
        multi = "문제:\n- [5] a — b\n  - 근거 1: x\n  - [9] 들여쓴 하위 줄\n\n- [2] c — d\n\n참고:\n- [3] e\nREVIEW-RESULT: issues=2\n"
        self.assertEqual(run.parse_review(multi), ({2, 5}, 2))
        self.assertEqual(run.parse_review("문제: 없음\n참고:\n- [3] 인용 일치\nREVIEW-RESULT: issues=0\n"), (set(), 0))

    # 무효(출력 형식)·맞음·FAIL을 가른다 — 기대 번호가 모두 있어야 맞음, 여분은 FAIL 아님, 음성은 늘 맞음, 형식이 먼저
    def test_b_judge_items(self):
        no_rr = REVIEW_OUT.replace("REVIEW-RESULT: issues=2\n", "")
        out = lambda *ns: "문제:\n" + "".join(f"- [{n}] /v/wiki/x.md:1 — a — b — c\n" for n in ns) + \
            f"REVIEW-RESULT: issues={len(ns)}\n"
        none = "문제: 없음\nREVIEW-RESULT: issues=0\n"
        for label, items, text, want in (
                ("RR 없음 — 번호가 맞아도 무효", [3], no_rr, ("무효", ["출력 형식"])),
                ("정확히 맞음", [3], out(3), ("맞음", [])),
                ("여분 번호", [3], out(2, 3, 5), ("맞음", [])),
                ("일부만 — 모두가 아님", [3, 5], out(3), ("FAIL", [])),
                ("문제 없음", [3], none, ("FAIL", [])),
                ("음성 — 지적 있어도 맞음", [], out(1, 4), ("맞음", [])),
                ("음성도 RR 없으면 무효", [], no_rr, ("무효", ["출력 형식"]))):
            with self.subTest(label):
                self.assertEqual(run.judge_items({"reviewer_items": items}, text), want)
        for label, exp, text, want in (
                ("any — 하나만 있어도 맞음", {"reviewer_items_any": [2, 3]}, out(3, 8), ("맞음", [])),
                ("any — 다른 하나", {"reviewer_items_any": [2, 3]}, out(2), ("맞음", [])),
                ("any — 하나도 없음", {"reviewer_items_any": [2, 3]}, out(5, 8), ("FAIL", [])),
                ("all + any — 둘 다 만족", {"reviewer_items": [8], "reviewer_items_any": [2, 3]}, out(3, 8), ("맞음", [])),
                ("all + any — all 빠짐", {"reviewer_items": [8], "reviewer_items_any": [2, 3]}, out(3), ("FAIL", [])),
                ("any — RR 없으면 무효", {"reviewer_items_any": [2, 3]}, no_rr, ("무효", ["출력 형식"]))):
            with self.subTest(label):
                self.assertEqual(run.judge_items(exp, text), want)

    # 변형 `### 기대`가 본 기대보다 먼저다 — 빼면 음성 변형(거짓-상충 본 [5] → 변형 [])이 FAIL로 뒤집힌다
    def test_b_judge_expected_variant(self):
        case = run.parse_case(case_text(B_FM, B_VARIANT_BODY))
        self.assertEqual(run.b_expected(case, None)["reviewer_items"], [5])
        self.assertEqual(run.b_expected(case, "음성")["reviewer_items"], [])
        self.assertEqual(run.b_expected(case, "기대없음")["reviewer_items"], [5])
        text = "문제:\n- [2] /v/wiki/x.md:1 — a — b — c\nREVIEW-RESULT: issues=1\n"
        self.assertEqual(run.judge_items(run.b_expected(case, "음성"), text)[0], "맞음")
        self.assertEqual(run.judge_items(run.b_expected(case, None), text)[0], "FAIL")


J_ROOT, J_VAULT = "/r", "/b/run/v1"     # 가짜 절대 경로 — 문자열만 다루니 실제 폴더가 없어도 된다


def tr(*recs):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)


def j_user(content=f"자료 넣기 작업을 검토해 줘.\n\n볼트 위치: {J_VAULT}/ (검토 범위: 이 폴더)\n"):
    return {"type": "user", "cwd": J_ROOT, "message": {"role": "user", "content": content}}


def j_tool(name, cwd=J_ROOT, type="assistant", **inp):
    rec = {"type": type, "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
    if cwd is not None:
        rec["cwd"] = cwd
    return rec


# 누출 검사는 transcript 문자열만 보는 일 함수로 직접 본다 — 기록 전에 부르는 순서·종료 코드 2 연결은 --judge(J10) 몫
class RunBJudgeLeakTest(unittest.TestCase):
    # 리뷰어가 실제 볼트(wiki/·raw/·루트 전체)나 실행 폴더의 다른 볼트·manifest·요청문을 보면 그 시도는 블라인드가 깨져 무효다.
    # 경로 경계(v1↔v10, wiki↔wiki-old)·`..`·cwd 없음·api-request-blob 중복도 함께 본다
    def test_b_leaks(self):
        bad = (
            ("Read 실제 wiki", j_tool("Read", file_path="/r/wiki/x.md"), "/r/wiki/x.md"),
            ("Read 실제 raw", j_tool("Read", file_path="/r/raw/a.md"), "/r/raw/a.md"),
            ("Grep 루트 전체", j_tool("Grep", pattern="x", path="/r"), "/r"),
            ("Grep path 없음 — cwd 기준", j_tool("Grep", pattern="x"), "/r"),
            ("Glob path 없음·cwd 없음 — --root 기준", j_tool("Glob", cwd=None, pattern="*.md"), "/r"),
            ("Read 상대 — cwd 루트", j_tool("Read", file_path="wiki/x.md"), "/r/wiki/x.md"),
            ("Glob 절대 pattern", j_tool("Glob", cwd=J_VAULT, pattern="/r/wiki/*.md"), "/r/wiki"),
            ("Glob 절대 pattern 볼트 전체", j_tool("Glob", cwd=J_VAULT, pattern="/r/**/*.md"), "/r"),
            ("다른 볼트", j_tool("Read", file_path="/b/run/v2/wiki/x.md"), "/b/run/v2/wiki/x.md"),
            ("manifest", j_tool("Read", file_path="/b/run/manifest.json"), "/b/run/manifest.json"),
            ("요청문", j_tool("Read", file_path="/b/run/v1.request.md"), "/b/run/v1.request.md"),
            ("상대 .. 로 다른 볼트", j_tool("Read", cwd=J_VAULT, file_path="../v2/raw/a.md"), "/b/run/v2/raw/a.md"),
            ("접두사 경계 v10", j_tool("Read", file_path="/b/run/v10/wiki/x.md"), "/b/run/v10/wiki/x.md"))
        for label, rec, path in bad:
            with self.subTest(label):
                leaks = run.transcript_leaks(tr(j_user(), rec), J_VAULT, J_ROOT)
                self.assertEqual(len(leaks), 1, leaks)
                self.assertTrue(leaks[0].endswith(" " + path), leaks)
        self.assertEqual(run.transcript_leaks(tr(j_user(), bad[0][1]), J_VAULT, J_ROOT), ["실제 볼트 /r/wiki/x.md"])
        self.assertEqual(run.transcript_leaks(tr(j_user(), bad[9][1]), J_VAULT, J_ROOT), ["실행 폴더 /b/run/manifest.json"])
        ok = (
            ("규칙 CLAUDE.md", j_tool("Read", file_path="/r/CLAUDE.md")),
            ("규칙 rules", j_tool("Read", file_path="/r/.claude/rules/페이지-형식.md")),
            ("리뷰어 정의", j_tool("Read", file_path="/r/.claude/agents/wiki-reviewer.md")),
            ("자기 볼트", j_tool("Read", file_path="/b/run/v1/wiki/x.md")),
            ("Grep 자기 볼트 전체", j_tool("Grep", pattern="x", path="/b/run/v1")),
            ("Read 상대 — cwd 자기 볼트", j_tool("Read", cwd=J_VAULT, file_path="wiki/x.md")),
            ("Glob 상대 pattern + 자기 볼트 path", j_tool("Glob", pattern="wiki/*.md", path=J_VAULT)),
            ("wiki-old는 wiki 안이 아님", j_tool("Read", file_path="/r/wiki-old/x.md")),
            ("api-request-blob은 보지 않음", j_tool("Read", type="api-request-blob", file_path="/r/wiki/x.md")))
        for label, rec in ok:
            with self.subTest(label):
                self.assertEqual(run.transcript_leaks(tr(j_user(), rec), J_VAULT, J_ROOT), [])
        two = j_tool("Read", file_path="/r/wiki/x.md")
        two["message"]["content"].append({"type": "tool_use", "name": "Read", "input": {"file_path": "/r/raw/a.md"}})
        self.assertEqual(run.transcript_leaks(tr(j_user(), two), J_VAULT, J_ROOT),
                         ["실제 볼트 /r/wiki/x.md", "실제 볼트 /r/raw/a.md"])

    # 다른 실행(다른 볼트)의 transcript로 판정하면 엉뚱한 볼트에 결과가 붙는다 — 첫 user 레코드에 이 볼트 경로가 있어야 한다
    def test_b_transcript_owner(self):
        read_ok = j_tool("Read", file_path="/b/run/v1/wiki/x.md")
        blocks = [{"type": "text", "text": f"볼트 위치: {J_VAULT}/ (검토 범위: 이 폴더)"}]
        for label, recs in (
                ("다른 볼트 v2", [j_user("볼트 위치: /b/run/v2/ …"), read_ok]),
                ("접두사 경계 v10", [j_user("볼트 위치: /b/run/v10/ …"), read_ok]),
                ("두 번째 user에만 있음", [j_user("볼트 위치: /b/run/v2/ …"), j_user(), read_ok]),
                ("user 레코드 없음", [read_ok])):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    run.transcript_leaks(tr(*recs), J_VAULT, J_ROOT)
        self.assertEqual(run.transcript_leaks(tr(j_user(blocks), read_ok), J_VAULT, J_ROOT), [])
        self.assertEqual(run.transcript_leaks(tr({"type": "summary"}, j_user(), read_ok), J_VAULT, J_ROOT), [])


S_POS = {"reviewer_items": [3], "must_detect": ["권고 강도 약화"], "must_not_detect": ["출처 간 상충"]}
S_NEG = {"reviewer_items": [], "must_not_detect": ["상충 표시 누락"]}     # 거짓-상충 변형 모양 — must_detect 키 없음
S_BARE = {"reviewer_items": [3]}                                          # 의미 대조가 필요 없음


# 의미 대조는 세션이 하고 --judge는 그 결과 인자만 본다 — 인자 → PASS/FAIL/대조 필요와 출력 줄을 일 함수로 직접 본다.
# 종료 코드 3·history·manifest에 쓰지 않음은 --judge(J10) 몫
class RunBJudgeSemanticTest(unittest.TestCase):
    # 필요한 인자가 하나라도 없으면 대조 필요(None) — 하나만 준 subTest가 all/any 변이를 막는다.
    # 빈 목록(키 없음·[])에 해당하는 인자는 값이 무엇이든 무시해야 음성 변형이 --detect 없이 PASS한다
    def test_b_semantic_verdict(self):
        for exp, detect, not_detect, want in (
                (S_POS, None, None, None),
                (S_POS, "예", None, None),
                (S_POS, None, "없음", None),
                (S_POS, "예", "없음", "PASS"),
                (S_POS, "아니오", "없음", "FAIL"),
                (S_POS, "예", "있음", "FAIL"),
                (S_NEG, None, None, None),
                (S_NEG, None, "없음", "PASS"),
                (S_NEG, "아니오", "없음", "PASS"),
                (S_NEG, None, "있음", "FAIL"),
                (S_BARE, None, None, "PASS"),
                (S_BARE, "아니오", "있음", "PASS"),
                ({"reviewer_items": [3], "must_detect": [], "must_not_detect": ["x"]}, None, "없음", "PASS")):
            with self.subTest(exp=exp, detect=detect, not_detect=not_detect):
                self.assertEqual(run.judge_semantic(exp, detect, not_detect), want)

    # 세션이 대조할 재료(기대 문장·줄머리 문제 줄)만 내고, 본문 속 `- [N]`·요약 줄·REVIEW-RESULT는 섞지 않는다.
    # 마지막 줄은 세션이 멈출 자리를 알아보는 표지라 정확히 고정한다
    def test_b_semantic_request(self):
        noisy = REVIEW_OUT.replace("문제:\n", "문제:\n- 본문 속 - [7] 언급\n  - [8] 들여씀\n- [항목번호] 자리표시\n- [3a] 정수 아님\n")
        lines = run.semantic_request("권고강도-보존", S_POS, noisy)
        for want in ("must_detect: 권고 강도 약화", "must_not_detect: 출처 간 상충",
                     "- [3] /v/wiki/x.md:5 — 권고 강도 약화 — \"must pin\" — 원문대로",
                     "- [2] /v/wiki/y.md:2 — 수치 다름 — \"3개\" — 고친다"):
            self.assertIn(want, lines)
        self.assertEqual(lines[-1], "판정: 권고강도-보존 세션 대조 필요")
        for bad in ("의미 검토", "REVIEW-RESULT", "[7]", "[8]", "[항목번호]", "[3a]"):
            self.assertFalse([l for l in lines if bad in l], bad)
        neg = run.semantic_request("거짓-상충--음성", S_NEG, REVIEW_OUT)
        self.assertFalse([l for l in neg if l.startswith("must_detect")])
        self.assertIn("must_not_detect: 상충 표시 누락", neg)
        self.assertEqual(neg[-1], "판정: 거짓-상충--음성 세션 대조 필요")


J_FAIL_OUT = "의미 검토 — 1:✅ 2:⚠️1건\n문제:\n- [2] /v/wiki/y.md:2 — 수치 다름 — \"3개\" — 고친다\nREVIEW-RESULT: issues=1\n"
J_NO_RR = REVIEW_OUT.replace("REVIEW-RESULT: issues=2\n", "")      # 출력 형식 무효
J_SEM_OK = ("--detect", "예", "--not-detect", "없음")                # B_BODY 기대(must_detect·must_not_detect 둘 다)가 PASS인 인자


# --judge는 J07~J09 일 함수를 잇고 회차·기록을 맡는다 — 가짜 볼트·임시 --base·가짜 regress로 CLI 전체를 돌린다.
# 상속하지 않는다 — 상속하면 prepare 테스트가 이 클래스 setUp으로 한 번 더 돈다
class RunBJudgeRoundTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b = RunBSelectTest.b
    prepare = RunBPrepareVaultTest.prepare

    def setUp(self):
        RunBPrepareVaultTest.setUp(self)
        self.hist = os.path.join(self.regress, "history.jsonl")
        self.io = tempfile.mkdtemp()             # 리뷰어 출력·transcript는 실행 폴더 밖에 둔다
        self.addCleanup(shutil.rmtree, self.io)

    def ready(self, run_id, *units):
        code, _, err = self.prepare(run_id, *units)
        self.assertEqual(code, 0, err)
        os.remove(self.hist)                     # "선택됨" 줄을 빼고 판정 줄만 본다

    def vpath(self, run_id, name):
        return os.path.join(os.path.abspath(self.base), run_id, name)

    def judge(self, name, review, *extra, run_id="r1", transcript=None):
        V = self.vpath(run_id, name)
        if transcript is None:
            transcript = tr(j_user(f"자료 넣기 작업을 검토해 줘.\n\n볼트 위치: {V}/ (검토 범위: 이 폴더)\n"),
                            j_tool("Read", cwd=self.root, file_path=f"{V}/wiki/요약-원문.md"))
        out, trp = os.path.join(self.io, f"{run_id}-{name}.out"), os.path.join(self.io, f"{run_id}-{name}.jsonl")
        for p, t in ((out, review), (trp, transcript)):
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
        return self.b("--judge", run_id, name, out, trp, *extra, "--root", self.root, "--base", self.base)

    def manifest(self, run_id="r1"):
        with open(os.path.join(self.base, run_id, "manifest.json"), encoding="utf-8") as f:
            return json.load(f)

    def rows(self):
        with open(self.hist, encoding="utf-8") as f:
            return [r for r in map(json.loads, f.read().splitlines()) if r["event"] == "판정"]

    def dirs(self, run_id="r1"):
        d = os.path.join(self.base, run_id)
        return sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n)))

    # 1회차 PASS는 최종 PASS — 새 볼트 없이 끝나고, 판정 기록은 spec J10-6 칸 그대로여야 뒤에서 이력을 잇는다
    def test_b_judge_pass_round1(self):
        self.ready("r1", "b-two")
        code, lines, err = self.judge("v1", REVIEW_OUT, *J_SEM_OK)
        self.assertEqual(code, 0, err)
        self.assertEqual(lines[-1], "판정: b-two 1회차 PASS — 최종 PASS")
        self.assertEqual(self.dirs(), ["v1"])
        self.assertEqual(self.manifest()["v1"]["result"], "PASS")
        self.assertEqual(self.rows(), [{
            "event": "판정", "date": datetime.date.today().isoformat(), "run_id": "r1", "vault": "v1",
            "unit": "b-two", "case": "b-two", "variant": None, "round": 1, "attempt": 1, "result": "PASS",
            "final": "PASS", "items_expected": [3], "items_expected_any": [], "items_found": [2, 3], "leaks": [], "detect": "예",
            "not_detect": "없음"}])

    # 1회차 FAIL은 한 번 더 돌려 FLAKY와 FAIL을 가른다 — 2회차 볼트·요청문을 만들고 3으로 세션에 재실행을 알린다
    def test_b_judge_fail_then_rerun(self):
        self.ready("r1", "b-two")
        code, lines, err = self.judge("v1", J_FAIL_OUT, *J_SEM_OK)
        self.assertEqual(code, 3, err)
        req = os.path.join(os.path.abspath(self.base), "r1", "v2.request.md")
        self.assertIn(f"재실행 요청문: {req}", lines)
        self.assertEqual(run.read(req), run.request_text(self.vpath("r1", "v2"), ["wiki/요약-원문.md"]))
        self.assertEqual(lines[-1], "판정: b-two 1회차 FAIL — 재실행")
        self.assertEqual(self.dirs(), ["v1", "v2"])
        m = self.manifest()
        self.assertEqual(m["v1"]["result"], "FAIL")
        self.assertEqual(m["v2"], {"unit": "b-two", "case": "b-two", "variant": None, "round": 2, "attempt": 1, "result": None})
        (row,) = self.rows()
        self.assertEqual((row["result"], row["final"], row["items_found"]), ("FAIL", None, [2]))
        # 의미 대조 FAIL도 항목 FAIL과 같은 회차 규칙
        self.ready("r-sem", "b-two")
        code, lines, _ = self.judge("v1", REVIEW_OUT, "--detect", "아니오", "--not-detect", "없음", run_id="r-sem")
        self.assertEqual((code, lines[-1]), (3, "판정: b-two 1회차 FAIL — 재실행"))
        for run_id, review, want_code, final, last in (("r2", REVIEW_OUT, 0, "FLAKY", "판정: b-two 2회차 PASS — 최종 FLAKY"),
                                                        ("r3", J_FAIL_OUT, 1, "FAIL", "판정: b-two 2회차 FAIL — 최종 FAIL")):
            with self.subTest(final):
                self.ready(run_id, "b-two")
                self.assertEqual(self.judge("v1", J_FAIL_OUT, *J_SEM_OK, run_id=run_id)[0], 3)
                code, lines, err = self.judge("v2", review, *J_SEM_OK, run_id=run_id)
                self.assertEqual(code, want_code, err)
                self.assertEqual(lines[-1], last)
                self.assertEqual(self.dirs(run_id), ["v1", "v2"])      # 2회차 뒤에는 새 볼트 없음
                self.assertEqual(self.manifest(run_id)["v2"]["result"], "PASS" if final == "FLAKY" else "FAIL")
                self.assertEqual([(r["vault"], r["round"], r["final"]) for r in self.rows()], [("v1", 1, None), ("v2", 2, final)])
        # 변형 유닛의 재실행 볼트도 변형 파일로 덮어써야 같은 유닛을 다시 돈 것이다
        self.ready("r4", "b-one--a-뒤")
        self.assertEqual(self.judge("v1", J_FAIL_OUT, *J_SEM_OK, run_id="r4")[0], 3)
        self.assertEqual(run.read(os.path.join(self.vpath("r4", "v2"), "wiki", "요약-원문.md")), "뒤\n")
        self.assertEqual(self.manifest("r4")["v2"], {"unit": "b-one--a-뒤", "case": "b-one", "variant": "a-뒤",
                                                     "round": 2, "attempt": 1, "result": None})

    # 무효는 리뷰어 탓이 아니라 시도 탓 — 회차를 넘기지 않고 같은 회차에서 attempt만 올려 다시 돈다
    def test_b_judge_invalid_retry(self):
        self.ready("r1", "b-two")
        leak = tr(j_user(f"볼트 위치: {self.vpath('r1', 'v1')}/ …"), j_tool("Read", cwd=self.root, file_path=f"{self.root}/wiki/x.md"))
        code, lines, err = self.judge("v1", REVIEW_OUT, *J_SEM_OK, transcript=leak)
        self.assertEqual(code, 3, err)
        self.assertEqual(lines[-1], "판정: b-two 무효 — 재실행")
        self.assertIn(f"재실행 요청문: {os.path.join(os.path.abspath(self.base), 'r1', 'v2.request.md')}", lines)
        m = self.manifest()
        self.assertEqual((m["v1"]["result"], m["v2"]["round"], m["v2"]["attempt"], m["v2"]["result"]), ("무효", 1, 2, None))
        (row,) = self.rows()
        self.assertEqual((row["result"], row["final"], row["leaks"]), ("무효", None, [f"실제 볼트 {self.root}/wiki/x.md"]))
        # 출력 형식 무효도 같은 길 — 사유는 "출력 형식"
        self.ready("r2", "b-two")
        code, lines, _ = self.judge("v1", J_NO_RR, *J_SEM_OK, run_id="r2")
        self.assertEqual((code, lines[-1]), (3, "판정: b-two 무효 — 재실행"))
        self.assertEqual(self.rows()[0]["leaks"], ["출력 형식"])
        self.assertEqual((self.manifest("r2")["v2"]["round"], self.manifest("r2")["v2"]["attempt"]), (1, 2))
        # 2회차 무효는 2회차에 머문다 — round를 1로 되돌리면 FLAKY·FAIL 판단이 틀어진다
        self.ready("r3", "b-two")
        self.assertEqual(self.judge("v1", J_FAIL_OUT, *J_SEM_OK, run_id="r3")[0], 3)
        self.assertEqual(self.judge("v2", J_NO_RR, *J_SEM_OK, run_id="r3")[0], 3)
        self.assertEqual({k: self.manifest("r3")["v3"][k] for k in ("round", "attempt")}, {"round": 2, "attempt": 2})

    # 무효가 반복되면 리뷰어 호출 환경이 고장 난 것 — 같은 유닛·회차 3번째 무효에서 새 볼트 없이 멈춘다
    def test_b_judge_invalid_third_stops(self):
        self.ready("r1", "b-two")
        self.assertEqual(self.judge("v1", J_NO_RR, *J_SEM_OK)[0], 3)
        self.assertEqual(self.judge("v2", J_NO_RR, *J_SEM_OK)[0], 3)
        code, lines, err = self.judge("v3", J_NO_RR, *J_SEM_OK)
        self.assertEqual(code, 1, err)
        self.assertEqual(lines[-1], "판정: b-two 무효 반복 — 중단")
        self.assertFalse([l for l in lines if l.startswith("재실행 요청문:")])
        self.assertEqual(self.dirs(), ["v1", "v2", "v3"])
        self.assertNotIn("v4", self.manifest())
        self.assertEqual([(r["vault"], r["attempt"], r["result"], r["final"]) for r in self.rows()],
                         [("v1", 1, "무효", None), ("v2", 2, "무효", None), ("v3", 3, "무효", None)])
        # 다른 회차의 무효는 세지 않는다 — 1회차 무효 둘 + 2회차 무효 하나는 아직 재실행
        self.ready("r2", "b-two")
        self.assertEqual(self.judge("v1", J_NO_RR, *J_SEM_OK, run_id="r2")[0], 3)
        self.assertEqual(self.judge("v2", J_NO_RR, *J_SEM_OK, run_id="r2")[0], 3)
        self.assertEqual(self.judge("v3", J_FAIL_OUT, *J_SEM_OK, run_id="r2")[0], 3)
        code, lines, _ = self.judge("v4", J_NO_RR, *J_SEM_OK, run_id="r2")
        self.assertEqual((code, lines[-1]), (3, "판정: b-two 무효 — 재실행"))
        self.assertEqual({k: self.manifest("r2")["v5"][k] for k in ("round", "attempt")}, {"round": 2, "attempt": 2})

    # 판정할 수 없는 입력은 아무것도 기록하지 않고 2 — 이유 낱말로 우연한 2(사용법·아직 구현되지 않음)와 가른다
    def test_b_judge_exit2_no_record(self):
        self.ready("r1", "b-two", "b-one")
        self.assertEqual(self.judge("v1", REVIEW_OUT, *J_SEM_OK)[0], 0)
        os.remove(self.hist)
        os.makedirs(os.path.join(self.base, "r-empty"))
        mpath = os.path.join(self.base, "r1", "manifest.json")
        with open(mpath, "rb") as f:
            before = f.read()
        other = tr(j_user(f"볼트 위치: {self.vpath('r1', 'v1')}/ …"))
        for label, go, why in (
                ("이미 결과 있음", lambda: self.judge("v1", REVIEW_OUT, *J_SEM_OK), "이미"),
                ("manifest에 없는 볼트", lambda: self.judge("v9", REVIEW_OUT, *J_SEM_OK), "manifest에 없는 볼트"),
                ("실행 폴더 없음", lambda: self.judge("v1", REVIEW_OUT, *J_SEM_OK, run_id="r-none"), "실행 폴더 없음"),
                ("manifest 없음", lambda: self.judge("v1", REVIEW_OUT, *J_SEM_OK, run_id="r-empty"), "manifest 없음"),
                ("다른 볼트의 transcript", lambda: self.judge("v2", REVIEW_OUT, *J_SEM_OK, transcript=other), "다른 실행"),
                ("리뷰어 출력 파일 없음", lambda: self.b("--judge", "r1", "v2", os.path.join(self.io, "없는출력.txt"),
                                                  os.path.join(self.io, "없는출력.txt"), "--root", self.root, "--base", self.base),
                 "없는출력.txt")):
            with self.subTest(label):
                code, lines, err = go()
                self.assertEqual(code, 2, lines)
                self.assertIn(why, err)
                self.assertFalse([l for l in lines if l.startswith("판정:")], lines)
                self.assertFalse(os.path.exists(self.hist))
                with open(mpath, "rb") as f:
                    self.assertEqual(f.read(), before)
                self.assertEqual(self.dirs(), ["v1", "v2"])

    # 세션 대조 필요는 결과가 아니다 — 기록·새 볼트 없이 3으로 멈추고, 세션이 인자와 다시 부르면 그때 판정한다.
    # 순서는 누출 → 항목 → 의미: 무효·항목 FAIL이면 인자가 없어도 대조 필요가 아니다
    def test_b_judge_semantic_needed(self):
        self.ready("r1", "b-two")
        mpath = os.path.join(self.base, "r1", "manifest.json")
        with open(mpath, "rb") as f:
            before = f.read()
        code, lines, err = self.judge("v1", REVIEW_OUT)
        self.assertEqual(code, 3, err)
        exp = run.b_expected(run.parse_case(run.read(os.path.join(self.regress, "cases", "b-two.md"))), None)
        self.assertEqual(lines, run.semantic_request("b-two", exp, REVIEW_OUT))
        self.assertFalse(os.path.exists(self.hist))
        with open(mpath, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.dirs(), ["v1"])
        code, lines, err = self.judge("v1", REVIEW_OUT, *J_SEM_OK)
        self.assertEqual((code, lines[-1]), (0, "판정: b-two 1회차 PASS — 최종 PASS"), err)
        self.ready("r2", "b-two")
        code, lines, _ = self.judge("v1", J_FAIL_OUT, run_id="r2")
        self.assertEqual((code, lines[-1]), (3, "판정: b-two 1회차 FAIL — 재실행"))
        self.ready("r3", "b-two")
        leak = tr(j_user(f"볼트 위치: {self.vpath('r3', 'v1')}/ …"), j_tool("Read", cwd=self.root, file_path=f"{self.root}/raw/a.md"))
        code, lines, _ = self.judge("v1", REVIEW_OUT, run_id="r3", transcript=leak)
        self.assertEqual((code, lines[-1]), (3, "판정: b-two 무효 — 재실행"))


# 상속하지 않는다 — 루트에 위키·영수증을 더한 픽스처로 다른 클래스의 테스트가 다시 돌면 안 된다
# 위치 인자 2개 --judge: 세션 기록 폴더에서 그 볼트의 리뷰어 호출을 찾아 출력을 꺼낸다 — 빈 출력을 넘겨
# "무효(출력 형식)"가 기록되고 재실행 볼트가 생기던 실수(2026-10-19 6c v1~v3)를 막는 것이 핵심
class RunBJudgeAutoTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b, prepare = RunBSelectTest.b, RunBPrepareVaultTest.prepare
    ready, vpath, manifest, rows, dirs = (RunBJudgeRoundTest.ready, RunBJudgeRoundTest.vpath, RunBJudgeRoundTest.manifest,
                                          RunBJudgeRoundTest.rows, RunBJudgeRoundTest.dirs)

    def setUp(self):
        RunBJudgeRoundTest.setUp(self)
        self.projects = os.path.join(self.io, "projects")

    def call(self, name, recs, sess="s1", agent="a1", meta=None, mtime=None, run_id="r1"):
        d = os.path.join(self.projects, sess, "subagents")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"agent-{agent}.jsonl")
        V = self.vpath(run_id, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(tr(j_user(f"자료 넣기 작업을 검토해 줘.\n\n볼트 위치: {V}/ (검토 범위: 이 폴더)\n"), *recs))
        if meta is not False:
            with open(p[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
                json.dump(meta or {"agentType": "wiki-reviewer", "finished": True}, f)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def handback(self, text):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "SubagentHandback", "input": {"message": text}}]}}

    def judge(self, name, *extra, run_id="r1"):
        return self.b("--judge", run_id, name, "--projects", self.projects, *extra, "--root", self.root, "--base", self.base)

    # SubagentHandback message가 출력 — 4개 인자 판과 같은 판정·기록, 첫 줄은 찾은 transcript
    def test_b_judge_auto_handback(self):
        self.ready("r1", "b-two")
        p = self.call("v1", [self.handback(REVIEW_OUT)])
        code, lines, err = self.judge("v1", *J_SEM_OK)
        self.assertEqual(code, 0, err)
        self.assertEqual(lines[0], f"transcript: {p}")
        self.assertEqual(lines[-1], "판정: b-two 1회차 PASS — 최종 PASS")
        self.assertEqual(self.rows()[0]["items_found"], [2, 3])

    # handback이 없으면 REVIEW-RESULT가 든 text, 둘 다 있으면 마지막 것
    def test_b_judge_auto_text_and_last(self):
        text = {"type": "assistant", "message": {"content": [{"type": "text", "text": J_FAIL_OUT}]}}
        self.assertEqual(run.review_output(tr(text)), J_FAIL_OUT)
        self.assertEqual(run.review_output(tr(text, self.handback(REVIEW_OUT))), REVIEW_OUT)
        self.assertEqual(run.review_output(tr(self.handback(REVIEW_OUT), text)), J_FAIL_OUT)
        self.assertEqual(run.review_output(tr({"type": "assistant", "message": {"content": [{"type": "text", "text": "중간"}]}})), "")

    # 다른 볼트(v1이 v10을 잡지 않게)·다른 에이전트는 고르지 않고, 같은 볼트의 호출이 여럿이면 가장 최근 것
    def test_b_judge_auto_pick(self):
        self.ready("r1", "b-two")
        now = time.time()
        self.call("v10", [self.handback(J_FAIL_OUT)], agent="x10")
        self.call("v1", [self.handback(J_FAIL_OUT)], agent="other", meta={"agentType": "Explore", "finished": True}, mtime=now + 50)
        self.call("v1", [self.handback(J_FAIL_OUT)], agent="old", mtime=now - 100)
        new = self.call("v1", [self.handback(REVIEW_OUT)], sess="s2", agent="new", meta=False, mtime=now)
        self.assertEqual(run.find_call(self.vpath("r1", "v1"), self.projects), new)
        code, lines, err = self.judge("v1", *J_SEM_OK)
        self.assertEqual((code, lines[0]), (0, f"transcript: {new}"), err)

    # 못 찾음·실행 중·출력에 REVIEW-RESULT 없음 → 2, manifest·history에 아무것도 안 씀(재실행 볼트도 안 생김)
    def test_b_judge_auto_refuse(self):
        self.ready("r1", "b-two")
        before = run.read(os.path.join(self.base, "r1", "manifest.json"))
        cases = (("없음", None, "리뷰어 호출 없음"),
                 ("실행 중", lambda: self.call("v1", [], meta={"agentType": "wiki-reviewer", "finished": False}), "아직 실행 중"),
                 ("빈 출력", lambda: self.call("v1", [self.handback("검토 중…")]), "REVIEW-RESULT 없음"))
        for label, setup, why in cases:
            with self.subTest(label):
                if setup:
                    setup()
                code, _, err = self.judge("v1", *J_SEM_OK)
                self.assertEqual(code, 2)
                self.assertIn(why, err)
                self.assertEqual(run.read(os.path.join(self.base, "r1", "manifest.json")), before)
                self.assertFalse(os.path.exists(self.hist))
                self.assertEqual(self.dirs(), ["v1"])

    # 사용법: 위치 인자 3개, 4개 인자에 --projects
    def test_b_judge_auto_usage(self):
        self.ready("r1", "b-two")
        self.assertEqual(self.b("--judge", "r1", "v1", "x", "--root", self.root, "--base", self.base)[0], 2)
        code, _, err = self.b("--judge", "r1", "v1", "o", "t", "--projects", self.projects, "--root", self.root, "--base", self.base)
        self.assertEqual(code, 2)
        self.assertIn("--projects", err)

    def test_b_projects_dir(self):
        self.assertEqual(run.projects_dir("/home/a/Obsidian"), os.path.expanduser("~/.claude/projects/-home-a-Obsidian"))
        self.assertEqual(run.projects_dir("/tmp/위키 x"), os.path.expanduser("~/.claude/projects/-tmp----x"))


class RunBStateTest(unittest.TestCase):
    tearDown, write, add = ValidateTest.tearDown, ValidateTest.write, ValidateTest.add
    b = RunBSelectTest.b
    prepare = RunBPrepareVaultTest.prepare
    ready, vpath, judge = RunBJudgeRoundTest.ready, RunBJudgeRoundTest.vpath, RunBJudgeRoundTest.judge

    def setUp(self):
        RunBJudgeRoundTest.setUp(self)
        for d in ("wiki", "raw", os.path.join("src", "x")):     # 위 setUp들은 이 폴더를 만들지 않는다
            os.makedirs(os.path.join(self.root, d))
        for p, t in (("wiki/index.md", "root-wiki\n"), ("raw/2026-10-01_x.md", "root-raw\n"),
                     (".claude/.lint_state.json", '{"keep": 1}'), ("src/x/a.md", "#\n")):
            self.write(p, t)

    def tree(self):
        # 루트 전체 경로→바이트. history.jsonl만 쓰여도 되는 파일이라 뺀다. .git은 없지만 있어도 index가 바뀔 수 있어 뺀다
        hist = os.path.relpath(os.path.join(self.regress, "history.jsonl"), self.root)
        out = {}
        for d, dirs, names in os.walk(self.root):
            dirs[:] = [x for x in dirs if x != ".git"]
            for n in names:
                p = os.path.join(d, n)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.root)] = f.read()
        out.pop(hist, None)
        return out

    # 회귀 실행이 실제 볼트의 위키·raw·영수증·시스템 파일을 건드리면 그 자체가 사고다 — 성공·실패 경로 모두 루트는 그대로,
    # 실행 폴더는 --base에만
    def test_b_prepare_judge_root_unchanged(self):
        before = self.tree()
        leak = lambda: tr(j_user(f"볼트 위치: {self.vpath('r2', 'v1')}/ …"),
                          j_tool("Read", cwd=self.root, file_path=f"{self.root}/wiki/index.md"))
        for label, go, want in (
                ("prepare 유닛", lambda: self.prepare("r1", "b-two", "b-one--a-뒤"), 0),
                ("prepare --files", lambda: self.prepare("r2", "--files", "src/x/a.md"), 0),
                ("prepare 이미 있음", lambda: self.prepare("r1", "b-two"), 2),
                ("prepare 없는 유닛", lambda: self.prepare("r9", "없는-케이스"), 2),
                ("judge 세션 대조 필요", lambda: self.judge("v1", REVIEW_OUT), 3),
                ("judge PASS", lambda: self.judge("v1", REVIEW_OUT, *J_SEM_OK), 0),
                ("judge 1회차 FAIL", lambda: self.judge("v2", J_FAIL_OUT, *J_SEM_OK), 3),
                ("judge 누출 무효", lambda: self.judge("v1", REVIEW_OUT, *J_SEM_OK, run_id="r2", transcript=leak()), 3),
                ("judge manifest에 없는 볼트", lambda: self.judge("v9", REVIEW_OUT, *J_SEM_OK), 2)):
            with self.subTest(label):
                code, lines, err = go()
                self.assertEqual(code, want, (lines, err))
                after = self.tree()
                stray = [k for k in after if os.path.basename(k) in ("manifest.json", "v1")
                         or k.endswith(".request.md") or os.path.basename(os.path.dirname(k)) == "v1"]
                self.assertEqual(stray, [])                       # 실패 메시지를 가르려고 따로 — 아래 동일성에도 들어 있다
                self.assertEqual(after, before)
        self.assertEqual(sorted(os.listdir(self.base)), ["r1", "r2"])


# 전역 사용자·서명 설정에 흔들리지 않게 -c로 고정한다. 실제 볼트·worktree가 아닌 임시 저장소에만 쓴다
def git_in(tmp, *a):
    return subprocess.run(["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *a], check=True, capture_output=True, text=True).stdout


class GitChangedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()                # git 저장소 밖 tempfile
        self.addCleanup(shutil.rmtree, self.tmp)
        git_in(self.tmp, "init", "-q")
        body = "".join(f"줄 {i} — rename 감지가 되게 내용을 둔다\n" for i in range(10))
        for p, t in (("a.md", "a\n"), ("s.md", "s\n"), ("del.md", "d\n"), ("old.md", body),
                     ("한글-케이스.md", body.replace("줄", "행")), ("한글-수정.md", "k\n"), (".gitignore", "ign.md\n")):
            self.write(p, t)
        git_in(self.tmp, "add", "-A")
        git_in(self.tmp, "commit", "-q", "-m", "base")
        self.base = git_in(self.tmp, "rev-parse", "HEAD").strip()

    def write(self, rel, text):
        with open(os.path.join(self.tmp, rel), "w", encoding="utf-8") as f:
            f.write(text)

    # 기본은 커밋 안 된 변경 전부 — 스테이징·작업 트리·지운 파일·추적 안 되는 새 파일, .gitignore 대상은 빼고
    def test_git_changed_head_default(self):
        self.write("a.md", "a2\n")
        self.write("s.md", "s2\n")
        git_in(self.tmp, "add", "s.md")
        os.remove(os.path.join(self.tmp, "del.md"))
        self.write("new.md", "n\n")
        self.write("ign.md", "i\n")
        self.assertEqual(sorted(run.git_changed(self.tmp)), ["a.md", "del.md", "new.md", "s.md"])

    # rename의 옛 경로를 빼면 옛 경로에 기대던 케이스를 놓친다 — 옛·새 둘 다, 한 번씩
    # (copy `C`는 -M만으로는 git이 내지 않는다. 같은 분기 `("R", "C")`라 rename으로 본다)
    def test_git_changed_rename_both(self):
        git_in(self.tmp, "mv", "old.md", "renamed.md")
        got = run.git_changed(self.tmp)
        self.assertEqual(got.count("old.md"), 1, got)
        self.assertEqual(got.count("renamed.md"), 1, got)

    # --since는 그 커밋 이후 커밋된 것 + 커밋 안 된 것 — 기본(HEAD)에는 커밋된 것이 들지 않는다
    def test_git_changed_since(self):
        self.write("c1.md", "c\n")
        git_in(self.tmp, "add", "c1.md")
        git_in(self.tmp, "commit", "-q", "-m", "c1")
        self.write("a.md", "a2\n")
        self.write("n.md", "n\n")
        got = run.git_changed(self.tmp, since=self.base)
        for p in ("c1.md", "a.md", "n.md"):
            self.assertIn(p, got)
        self.assertNotIn("c1.md", run.git_changed(self.tmp))

    # 케이스 파일 이름이 한글이다 — 따옴표·8진수(`"\355…"`)로 깨지면 케이스 파일 대조가 안 맞는다
    def test_git_changed_korean_path(self):
        self.write("한글-수정.md", "k2\n")
        self.write("새-파일.md", "n\n")
        git_in(self.tmp, "mv", "한글-케이스.md", "한글-이름.md")
        self.assertEqual(sorted(run.git_changed(self.tmp)),
                         sorted(["한글-수정.md", "새-파일.md", "한글-케이스.md", "한글-이름.md"]))


if __name__ == "__main__":
    unittest.main()
