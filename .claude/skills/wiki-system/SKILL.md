---
name: wiki-system
description: 위키 시스템 변경 절차 — 규칙(CLAUDE.md)·검사기(wiki_lint.py)·hook(settings.json)·스킬·rules·리뷰어를 바꿀 때 쓴다. git 운영(hook이 하는 커밋)의 세부, schema 진화 규칙도 여기 있다. "규칙 바꾸자", "검사기 고쳐", "hook 수정", "스킬 고쳐", "리뷰어 항목 바꿔"라고 할 때 쓴다.
---

# 시스템 변경

핵심: 규칙을 바꾸면 log.md에 `schema` 항목을 남긴다. 기존 페이지에 영향이 있으면 건강검진 때 일괄 반영한다. 끝내기 전에 `wiki-verify`를 돌린다.

## Schema 진화
- 사용자와 작업하며 더 나은 규칙이 생기면 `CLAUDE.md`(또는 해당 스킬·rules·리뷰어 파일)를 업데이트하고 log.md에 `schema` 항목으로 남긴다.
- 규칙 변경이 기존 페이지에 영향을 주면 건강검진 때 일괄 반영한다.
- 한 규칙은 한 곳에만 둔다. 매 세션 필요한 불변 규칙은 `CLAUDE.md`, 절차는 스킬, wiki 페이지 형식 세부는 `.claude/rules/페이지-형식.md`, 의미 검토 항목은 `.claude/agents/wiki-reviewer.md`.
- 검사기는 `CLAUDE.md`의 `현재 도메인: ` 줄을 읽는다. 이 줄의 형식은 바꾸지 않는다.

## git 운영 (hook이 하는 일)
- 세션 시작: 지난 커밋 이후의 변경은 사용자(Obsidian·Web Clipper)가 한 것이므로 "사용자 변경"으로 따로 커밋하고 파일 목록을 알려 준다. 목록의 wiki 페이지는 사용자 수정으로 보고 존중한다 (절대 규칙 3). 아직 넣지 않은 raw가 있으면 함께 알려 주니, 첫 요청을 처리한 뒤 "넣을까요?"라고 한 줄로 제안한다.
- 끝낼 때: Stop hook의 검증을 통과하면 이 세션 작업을 커밋한다 (제목 = 새로 추가한 log 항목). 5회 상한을 넘기면 `[검증 미통과 — 사용자 확인 필요]` 제목으로 커밋해, 다음 세션이 사용자 변경으로 오인하지 않게 한다. (한계: 세션이 열려 있는 동안 사용자가 Obsidian에서 고친 내용은 이 세션 커밋에 섞인다.)
- Claude는 `git commit`·`reset`·`checkout`·`rebase`·`push` 등을 직접 하지 않는다. 되돌리기가 필요하면 사용자에게 묻는다. 조회(`git log`·`diff`·`status`)는 해도 된다.


## 회귀 스위트
시스템 파일을 바꾼 뒤 관련 층을 돌린다. 명령·형식 세부는 `.claude/regress/README.md`.
- `.claude/wiki_lint.py`·`CLAUDE.md`를 바꾸면 `python3 .claude/regress/run.py A` (A층은 실제 `CLAUDE.md`를 복사해 쓴다).
  - Stop hook이 강제한다: 둘 중 하나라도 세션 시작 때와 내용이 다르면 A층이 통과할 때까지 끝내지 못한다(기존 5회 상한·미통과 커밋 그대로). 한계: 검사기에 문법 오류가 나면 hook 자체가 실패해 막지 못하니, 고친 뒤에는 직접 `run.py A`를 돌린다. `CLAUDE.md` 도메인 줄 붕괴는 A층이 아니라 기계 검사가 잡는다.
- `.claude/regress/`의 러너(`run.py`·`test_run.py`)나 케이스를 바꾸면 `python3 .claude/regress/test_run.py` → `run.py validate` → `run.py A`.
- 리뷰어 정의·`CLAUDE.md`·rules·스킬·B층 케이스를 바꾸면 `python3 .claude/regress/run.py B --select --files <바꾼 파일 …>`로 B층 케이스를 고른다. 0개면 B층은 돌리지 않는다.
- B층 선택에는 `--files`(또는 `--since <REF>`)를 쓴다 — 인자 없는 `--select`는 HEAD 대비 커밋 안 된 변경만 보는데 Stop hook이 턴마다 커밋해 다음 턴에는 빈 목록이 된다. `--files` 경로는 볼트 루트 기준 상대 경로로 쓴다(문자열로 비교 — `./`·절대 경로면 0개).
- B층 실행은 `.claude/regress/README.md` "B층 실행" 순서를 따른다. 뼈대: `run.py B --prepare <run-id> --files <바꾼 파일 …>`로 미니 볼트·요청문을 만들고 → 볼트마다 `v<k>.request.md`를 고치지 않고 `wiki-reviewer`에 넘기고(블라인드 — 케이스·기대를 덧붙이지 않음) → 리뷰어가 끝나면 `run.py B --judge <run-id> v<k>`(위치 인자 2개 — 러너가 transcript를 찾아 출력을 꺼냄). 종료 코드 3이면 README대로 세션 대조(`--detect`·`--not-detect`를 붙여 같은 볼트로)나 `재실행 요청문:`의 새 볼트로 되풀이한다.
- 리뷰어 정의(`.claude/agents/wiki-reviewer.md`)를 고친 턴에는 B층을 돌리지 않는다 — 같은 턴의 리뷰어 호출은 고치기 전 정의로 돈다(관찰, 기준: Claude Code 2.1.303 — 공식 문서의 "다음 위임부터 새 정의"와 다름). 그 턴의 log `schema` 항목에는 회귀를 "미실행 — 다음 턴"으로 적고, 다음 사용자 턴에 탐침 호출로 새 정의가 붙었는지 확인한 뒤 돌린다 — 탐침 요청문에 `/tmp/wiki-regress/probe-<이름>/` 경로를 넣고(첫 요청에 이 경로가 있으면 영수증이 남지 않는다) 고친 문구를 글자 그대로 옮기게 한다(출력 마지막 줄 `REVIEW-RESULT`는 그대로 남기게 — 없으면 SubagentStop이 한 번 막는다). `CLAUDE.md`·rules는 리뷰어가 매번 직접 Read하므로 이 제약이 없다.
- 회귀를 돌렸으면 log `schema` 항목에 `- 회귀:` 줄을 남긴다. 형식: `` - 회귀: A <PASS>/<실행> · run `<run-id>` — B <PASS+FLAKY>/<유닛 수> (PASS <n> · FLAKY <n> · FAIL <n>, 무효 <n>) ``. A는 `run.py A` 요약 줄로, B는 유닛마다 마지막 `판정:` 줄의 최종 결과로 센다(`무효 반복 — 중단`으로 끝난 유닛은 분모에 넣고 `무효`로 센다 — 중간 `무효 — 재실행`은 세지 않는다). 러너를 바꿨으면 앞에 `러너 테스트 OK · validate 0`도 적는다. 돌리지 않은 층은 이유를 적는다(예: `B 선택 0`, `B 미실행 — 다음 턴`). 최종 FAIL·FLAKY 유닛 이름(`<id>` 또는 `<id>--<변형>`)도 적는다(예: `FAIL: 상충-누락, FLAKY: 확실성-부풀림`).
- B층 최종 FAIL이면 고치기 전에 케이스의 raw 발췌·fixture부터 원문과 재대조해 원인을 가른다 — 회귀(fixture는 맞는데 리뷰어가 놓침 → 리뷰어 정의·스킬·rules를 고침) / 케이스 문제(fixture에 다른 결함이 섞였거나 기대가 틀림 → 케이스를 고침, 형식은 README "케이스 파일") / 정답이 갈림(→ 사용자에게 묻는다). 판정이 러너 탓이면 러너를 고치고 위 러너 불릿 순서를 따른다. 케이스 기대를 넓히기(`reviewer_items_any` 등)·규칙 추가처럼 판단이 필요한 처리는 사용자에게 묻는다.
- 고친 뒤에는 `run.py B --prepare <새 run-id> <유닛 …>`로 그 유닛만 다시 돌린다(run-id는 실행마다 새로, 리뷰어 정의를 고쳤으면 위 불릿대로 다음 턴). FLAKY(1회차 FAIL·2회차 PASS)는 회귀로 보지 않고 `- 회귀:` 줄에 기록만 한다 — 되풀이되면 건강검진(`wiki-lint`) 대상. 중간 `무효 — 재실행`은 결과가 아니다(러너가 `재실행 요청문:`을 만든다). `무효 반복 — 중단`이면 회귀로 보지 않고 `무효 사유:`를 보고에 적는다.
