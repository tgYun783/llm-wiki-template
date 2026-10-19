# 회귀 스위트

위키 내용이 아니라 **위키를 다루는 장치**(규칙 `CLAUDE.md`·rules, 검사기 `wiki_lint.py`, 스킬, `wiki-reviewer`)를 바꿨을 때, 예전에 고친 실수가 다시 나오는지 확인한다.

| 층 | 확인하는 것 | 실행 |
|---|---|---|
| A. 검사기 | `wiki_lint.py`가 잡을 것은 잡고, 잡지 말 것은 넘기는지 | 가짜 `wiki/`·`raw/` 묶음에 검사기 복사본 → 기대 출력과 비교 |
| B. 리뷰어 | `wiki-reviewer`가 과거의 실제 실수를 다시 잡는지 | 러너가 미니 볼트·요청문 생성 → 세션이 리뷰어 호출 → 러너가 대조 |

## 사용법
```
python3 .claude/regress/run.py validate   # registry·케이스 형식 검사
python3 .claude/regress/test_run.py       # 러너 자체의 단위 테스트
python3 .claude/regress/run.py A [<case> ...] [--root DIR] [--lint FILE]
                                          # A층 케이스 실행 (PASS/FAIL 줄 + 요약, 종료 코드 0 | 1 | 2)
                                          # --root: CLAUDE.md·검사기를 가져올 볼트 루트, --lint: 대신 쓸 검사기 파일
python3 .claude/regress/run.py B --select [--files <경로> ...] [--since <REF>] [--root DIR]
                                          # 바뀐 파일로 돌릴 B층 케이스를 고름 (`<id> ← <이유>` 줄 + 요약, 종료 코드 0 | 2)
                                          # --files 없으면 HEAD 대비 커밋 안 된 변경(+추적 안 되는 파일), --since: 그 커밋 이후
python3 .claude/regress/run.py B --prepare <run-id> [<유닛> ...] [--files <경로> ... | --since <REF>] [--root DIR] [--base DIR]
                                          # 유닛마다 미니 볼트 v<k>/·요청문 v<k>.request.md를 <base>/<run-id>/에 만듦 (`v<k> ← <유닛>` 줄 + 요약, 종료 코드 0 | 2)
                                          # 유닛: <id> 또는 <id>--<변형>. 없으면 --select와 같은 입력으로 고른 케이스의 본 케이스 + 변형 전부. --base 기본 /tmp/wiki-regress
python3 .claude/regress/run.py B --judge <run-id> <볼트> [<리뷰어 출력 파일> <transcript>] [--projects DIR] [--detect 예|아니오] [--not-detect 없음|있음] [--root DIR] [--base DIR]
                                          # 볼트 하나를 판정 (마지막 줄 `판정: <유닛> …`, 종료 코드 0 최종 PASS·FLAKY | 1 최종 FAIL·무효 반복 | 3 재실행·세션 대조 필요 | 2 판정 불가)
```

## B층 실행
리뷰어를 부르는 것은 세션이다. 러너는 준비와 판정만 하고 리뷰어를 부르지 않는다. 세션은 이 순서로 한다.

1. `run.py B --prepare <run-id> …`로 실행 폴더 `<base>/<run-id>/`에 볼트(`v1`, `v2`, …)와 요청문을 만든다. 볼트 이름은 불투명한 번호이고, 유닛은 `manifest.json`에 있다.
2. 볼트마다 `v<k>.request.md`를 고치지 않고 그대로 `wiki-reviewer`에 넘긴다 (요청문에 케이스·기대를 덧붙이지 않는다 — 블라인드).
3. 리뷰어가 끝나면 `run.py B --judge <run-id> v<k>`를 부른다. 러너가 세션 기록 폴더(`--projects`, 기본 `~/.claude/projects/<볼트 경로의 영숫자 밖 글자를 -로>`)의 `*/subagents/*.jsonl`에서 첫 요청에 그 볼트 경로가 있는 리뷰어 호출(가장 최근 것)을 찾아, 최종 출력(`SubagentHandback`의 `message`, 없으면 `REVIEW-RESULT`가 든 마지막 글)을 꺼내 판정하고 첫 줄에 `transcript: <경로>`를 찍는다. 호출이 없거나 아직 실행 중이거나 출력에 `REVIEW-RESULT`가 없으면 아무것도 기록하지 않고 2로 끝난다. 출력 파일과 transcript를 직접 주는 4개 인자 형식(`--judge <run-id> v<k> <리뷰어 출력 파일> <transcript>`)도 그대로 쓸 수 있다.
4. 마지막 줄이 `판정: <유닛> 세션 대조 필요`(종료 코드 3)면, 출력된 기대 문장(`must_detect: …`·`must_not_detect: …`)과 리뷰어 문제 줄을 세션이 대조하고 `--detect 예|아니오`·`--not-detect 없음|있음`을 붙여 같은 볼트로 다시 `--judge`한다.
5. `재실행 요청문: <경로>` 줄이 나오면(`판정: <유닛> 1회차 FAIL — 재실행`·`판정: <유닛> 무효 — 재실행`, 종료 코드 3) 그 요청문으로 2부터 되풀이한다.
6. 끝나는 줄: `판정: <유닛> <N>회차 <결과> — 최종 <최종>`(최종 PASS·FLAKY는 0, FAIL은 1), `판정: <유닛> 무효 반복 — 중단`(1). 무효면 그 앞에 `무효 사유: …` 줄이 있다. 2는 판정 불가(아무것도 기록하지 않음).

- 판정마다 `history.jsonl`에 한 줄이 쌓이고(세션 대조 필요는 제외) `manifest.json`의 그 볼트 `result`가 채워진다. `--prepare`도 유닛마다 "선택됨" 한 줄을 남긴다.
- `--prepare`·`--judge`를 실제 볼트로 돌리면 `/tmp/wiki-regress/`(기본 `--base`)와 `history.jsonl`에 흔적이 남는다. `history.jsonl`은 `--base`와 상관없이 `run.py` 옆에 쌓인다.

## 파일
```
.claude/regress/
  README.md        ← 이 파일
  registry.yaml    ← components → 파일 매핑, broad, failure_class 허용 목록
  cases/<id>.md    ← 케이스 1개 = 근본 실패 유형 1개 (+ 변형)
  run.py           ← 러너 (표준 라이브러리만)
  test_run.py      ← 러너 단위 테스트
```

## 형식 공통: YAML 부분집합
PyYAML 없이 읽으므로 아래 모양만 쓴다. 다른 모양은 validate 오류.
- `키: 값` — 값은 문자열, 숫자, `true`·`false`, `null`(또는 빈 값)
- 인라인 목록 `[a, b, 3]` — 쉼표가 든 항목은 `"…"`로 감싼다
- 빈 키 아래 `- 항목` 목록
- 빈 키 아래 한 단계 들여쓴 `키: 값` 맵
- 주석은 줄 전체(`# …`)만. 값 뒤의 `#`는 값의 일부다

## registry.yaml
```yaml
# 이름: [파일 glob, ...]  (** 재귀 가능)
components:
  wiki-reviewer: [.claude/agents/wiki-reviewer.md]
  wiki-ingest: [.claude/skills/wiki-ingest/**]
# 이것이 바뀌면 B층 전체 실행. components에 있는 이름만
broad: [wiki-reviewer]
# 허용 목록 — 새 값은 여기 먼저 추가
failure_classes:
  recommendation-strength-loss: 원문 권고 강도가 약해지거나 세짐
```
- component는 **파일 단위**다. 리뷰어 항목 단위(`wiki-reviewer:item-3`)는 diff→항목 매핑을 만들 때 추가한다. 그 전까지 항목 번호는 케이스의 `기대.reviewer_items`에만 둔다.
- glob에 맞는 파일이 없으면 경고(이름이 바뀐 파일).

## 케이스 파일 `cases/<id>.md`
**근본 실패 유형 하나에 케이스 하나.** 같은 유형의 다른 모습은 변형(`## 변형:`)으로 둔다.

````markdown
---
id: 권고강도-보존
층: B
failure_class: recommendation-strength-loss
원인판정: 절차
출처: ["[2026-10-05] ingest | 제목", "커밋 abc1234"]
components: [wiki-reviewer, wiki-ingest]
files: []
내릴 수 있나: 아니오
precheck: null
private: false
수정한 페이지: [wiki/요약-원문.md]
---

## 기대
reviewer_items: [3]
must_detect:
- 권고 강도가 원문보다 약화됨
must_not_detect:
- 출처 간 상충

## 파일: raw/2026-10-01_원문-발췌.md
~~~~
(미니 볼트의 raw/에 들어갈 내용)
~~~~

## 파일: wiki/요약-원문.md
~~~~
(미니 볼트의 wiki/에 들어갈 내용)
~~~~

## 변형: 한국어-원문
### 수정한 페이지
- wiki/요약-원문.md
### 파일: raw/2026-10-01_원문-발췌.md
~~~~
(같은 경로를 덮어쓴다. 안 적은 파일은 본 케이스 것을 쓴다)
~~~~

## 메모
(사람용 설명. 러너가 읽지 않는다 — 리뷰어에게도 넘어가지 않는다)
````

### 칸 규칙
| 칸 | 규칙 |
|---|---|
| frontmatter | 위 칸만 허용 (오타 방지). `id`·`층`·`failure_class`·`원인판정`·`출처`·`components`·`private` 필수, B층은 `내릴 수 있나`도 |
| `id` | 파일 이름(`.md` 뺀 것)과 같아야 함 |
| `층` | `A` \| `B` |
| `failure_class` | registry `failure_classes`의 값 |
| `원인판정` | `절차` \| `규칙 공백` (`wiki-verify` 2단계 판정. "정답이 갈림"은 케이스를 만들지 않음) |
| `출처` | 이 유형이 나온 작업 — log 항목 제목 또는 커밋. 1개 이상 |
| `runner` | A층만, 필수. 아래 "A층 runner" |
| `내릴 수 있나`·`precheck` | B층만. 아래 표. `precheck`는 `부분`일 때만, A층 케이스 id |
| `private` | 볼트 실례면 `true` (템플릿 동기화에서 제외) |
| `수정한 페이지` | B층만, 필수. 리뷰어 요청문에 "수정한 페이지"로 넘길 페이지 — 케이스의 `wiki/` 파일 칸 중에서. 나머지 `wiki/` 칸은 비교 대상(상충·들어오는 링크 판단용)으로 미니 볼트에만 들어간다 |
| `components`·`files` | 둘 다 비면 오류 (선택기가 고를 수 없음). `files`는 실제로 있어야 함 |
| `## 기대` (B층) | `reviewer_items`(all-of — 모두 보고돼야 함) 또는 `reviewer_items_any`(any-of — 하나 이상 보고되면 됨) 중 하나 이상 필수(음성은 `reviewer_items: []`). `reviewer_items_any`는 리뷰어가 같은 결함을 실행마다 다른 항목으로 보고할 때만 쓰고, 비우지 않는다. 둘 다 있으면 둘 다 만족해야 한다. 양성(어느 쪽이든 항목 있음)은 `must_detect`, 음성은 `must_not_detect`가 비면 오류. 항목 번호는 리뷰어 정의의 검토 항목 범위 안. 그 밖의 논리식은 두지 않는다 |
| `## 기대` (A층) | runner마다 다름 — 아래 "A층 runner" |
| `## 파일: <경로>` | 경로는 `wiki/<이름>` 또는 `raw/<이름>`만 (하위 폴더 없음 — 볼트 규칙과 같음. A층 `hook-review-done`은 `hook/<이름>`도). 내용은 제목 바로 아래 `~~~~` 울타리 안. 같은 경로 두 번 금지 |
| B층 | `wiki/` 파일 칸이 1개 이상 (검토할 페이지). 상충·들어오는 링크 판단(리뷰어 항목 5·8)이 필요하면 비교 대상 페이지도 넣는다 |
| `## 변형: <이름>` | 안에서는 `###` 제목. 파일 칸 1개 이상. `### 기대`가 없으면 본 케이스 기대를 쓴다. `### 수정한 페이지`(`- wiki/<이름>` 줄 목록, 본 케이스 칸 포함 가능)가 없으면 본 케이스 것을 쓴다 |
| `## 메모` | 자유 형식 |

- 울타리가 `~~~~`(물결 4개)인 것은 내용 안의 ```` ``` ```` 코드 블록과 겹치지 않게 하려는 것이다. 내용에 물결 4개만 있는 줄은 쓸 수 없다.
- 시스템 파일(`CLAUDE.md`·rules·리뷰어 정의)은 케이스에 넣지 않는다. 러너가 실행 시점의 현재 파일을 복사한다.

### A층 runner
검사기(`wiki_lint.py`)를 어떤 모드로 돌리는지 정한다. 러너는 임시 폴더에 파일 칸을 펼치고 그 안의 검사기 복사본으로 돌린다.

| runner | 돌리는 것 | 파일 칸 | 기대 |
|---|---|---|---|
| `lint` | `wiki_lint.py` (기계 검사) | `wiki/`·`raw/` | `must_output`(나와야 할 출력 줄의 일부)·`must_not_output`(나오면 안 되는 줄) 중 하나 이상 |
| `hook-review-done` | `wiki_lint.py --review-done` (SubagentStop hook) | `wiki/`·`raw/`·`hook/`. `hook/input.json`(hook stdin) 필수, `hook/transcript.jsonl` 등 선택 | `receipt` 필수 — 기록될 영수증의 issues 값, 영수증이 남지 않아야 하면 `null`. stdout은 `must_output`·`must_not_output`(선택) |

- `hook/input.json` 안의 `hook/…` 상대 경로는 러너가 임시 폴더의 절대 경로로 바꿔 넘긴다.

### 내릴 수 있나
| 값 | 의미 | 처리 |
|---|---|---|
| 예 | 결정적 검사만으로 최종 판정 가능 | A층으로 옮기고 B층 케이스 은퇴 (B층에 남아 있으면 validate 경고) |
| 부분 | 코드가 후보를 좁히고 최종 의미 판단은 LLM | B층 유지 + `precheck`에 짝 A층 케이스 |
| 아니오 | 의미 판단 전체가 LLM에 필요 | B층 유지 |
