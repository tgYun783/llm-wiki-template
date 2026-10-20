# 운영 메트릭 — raw 등록 비용

raw 자료를 위키에 넣는 작업 1회마다 걸린 시간·토큰·호출 수를 `ingest_history.jsonl`에 한 줄씩 쌓는다. 리뷰어·검증·회귀 스위트가 비용에서 차지하는 비중과, 시스템(규칙·스킬·리뷰어·모델)을 바꾼 전후의 비용을 나중에 비교하려는 것이다.

- 회귀 테스트 이력(`.claude/regress/history.jsonl`)과는 다른 파일이다. 섞지 않는다.
- `ingest_history.jsonl`은 append-only다. 기존 줄을 고치거나 지우지 않는다. git에 커밋한다.
- 원시 메트릭은 `wiki/log.md`에 옮기지 않는다. 표본이 쌓였거나 의미 있는 변화가 있을 때만 요약을 log에 남긴다.
- 측정이 또 하나의 복잡한 시스템이 되지 않게 한다. 항목은 데이터가 쌓인 뒤 필요한 것만 늘린다.

## 사용법
`wiki-ingest` 스킬이 부른다.
```
python3 .claude/metrics/ingest_metrics.py start <raw 파일…>     # 넣기 1단계(확인) 직후
python3 .claude/metrics/ingest_metrics.py finish <nonce> [--b-run <run-id> …] [--dry-run]   # 검증 루프가 끝난 뒤
python3 .claude/metrics/test_ingest_metrics.py                   # 스크립트 테스트
```
- `start`는 `METRIC-START {…}` 한 줄(nonce·시작 시각·raw 목록·문자 수·git HEAD)을 출력한다. 이 출력이 세션 기록에 남아 구간의 시작이 된다 — 상태 파일은 없다.
- `finish`는 Claude Code 세션 기록(`~/.claude/projects/<볼트 경로>/*.jsonl`과 그 `subagents/`)에서 그 표지를 찾아, 표지부터 지금까지를 합산한다. 다른 세션의 기록은 섞이지 않는다.
- 여러 건을 한 작업으로 넣으면 `start`에 raw를 모두 넘기고 끝에 한 번 `finish`한다(한 줄). 작업이 세션을 넘어가면 세션마다 그 세션에서 넣은 raw로 `start`·`finish`한다.
- 한계: `finish` 뒤의 일(Stop hook 커밋, 그 뒤에 생긴 차단과 수정)은 그 줄에 들어가지 않는다. 권한 승인을 기다린 시간은 기록에 남지 않아 `active_sec`에서 빠지지 않는다.

## 레코드 형식 (metric_schema_version 1)
```json
{"metric_schema_version":1,"timestamp":"2026-10-19T22:10:31+09:00","raw_id":["2026-10-18_example.md"],"raw_count":1,"raw_size":18420,"duration_sec":420,"active_sec":231,"input_tokens":4100000,"cache_creation_tokens":180000,"cache_read_tokens":3900000,"output_tokens":21000,"llm_calls":52,"review_calls":2,"retry_count":2,"B_selected":0,"B_result":{"pass":0,"flaky":0,"fail":0,"invalid":0},"git_head":"a8fa40a","model":{"main":"claude-opus-5-5","reviewer":"claude-opus-5-5"},"cc_version":"2.1.302","phases":{"review":{"duration_sec":180,"input_tokens":900000,"output_tokens":8000}}}
```

| 필드 | 뜻 |
|---|---|
| `metric_schema_version` | 이 형식의 버전. 필드를 더하거나 뜻을 바꾸면 올리고 아래 "버전 이력"에 적는다. 옛 줄은 고치지 않는다 |
| `timestamp` | 작업 종료 시각(`finish` 시각, 로컬 시간대) |
| `raw_id` · `raw_count` | 넣은 raw 파일명 목록과 개수 |
| `raw_size` | 원문 문자 수(characters) 합계. 바이트가 아님 |
| `duration_sec` | `start`부터 `finish`까지 실제 시간 |
| `active_sec` | `duration_sec`에서 사용자를 기다린 시간(사람 메시지 직전 작업 기록 — assistant·user·system — 부터의 간격, AskUserQuestion 답까지)을 뺀 것. 분석은 주로 이것으로 |
| `input_tokens` | 입력 합계 = 새 입력 + 캐시 생성 + 캐시 읽기 |
| `cache_creation_tokens` · `cache_read_tokens` | 입력 중 캐시 생성·캐시 읽기. 캐시 읽기는 호출마다 문맥 전체를 다시 읽은 양이라 크고 단가가 낮다 — 비용 비교는 세 종류를 나눠 본다 |
| `output_tokens` | 출력 합계. thinking 토큰이 섞여 있는 것으로 보인다(기록에서 따로 나뉘지 않음) |
| `llm_calls` | 메인 + 서브에이전트 전체 API 호출 수 (응답 id 기준 — 한 응답이 여러 레코드로 나뉘어도 한 번) |
| `review_calls` | 구간 안에서 시작한 `wiki-reviewer` 호출 수 |
| `retry_count` | 리뷰어 재검토(`review_calls - 1`, 최소 0) + Stop hook 차단 횟수. 기계 검사 재실행은 세지 않는다 |
| `B_selected` · `B_result` | `--b-run`으로 넘긴 회귀 run의 유닛 수와 유닛별 마지막 최종 판정(pass·flaky·fail·invalid). 넘기지 않으면 0 — 넣기는 보통 시스템 파일을 바꾸지 않아 0이 정상 |
| `git_head` | `start` 시점 HEAD 짧은 해시 — 그 시점의 규칙·스킬·리뷰어를 git으로 되짚는다 |
| `model` | `main`·`reviewer` 모델 ID (여럿이면 목록) |
| `cc_version` | Claude Code 버전 (구간 마지막 레코드 기준) |
| `phases` | 있을 때만. 지금은 `review`(리뷰어 호출들의 시간·입력·출력 합)만 |

## 나중에 볼 지표
raw 1건당 평균·중앙·p90 `active_sec`, 1건당 입력·출력 토큰, `raw_size` 대비 토큰, 리뷰어 비용 비중(`phases.review` ÷ 전체), 회귀 비용 비중, 평균 `B_selected`, retry 비율, FLAKY 비율, `git_head`·`model`·`cc_version` 구간별 비용 변화.

```
python3 -c "import json,statistics as s; r=[json.loads(l) for l in open('.claude/metrics/ingest_history.jsonl')]; print(len(r), s.median(x['active_sec']/x['raw_count'] for x in r))"
```

## 버전 이력
- 1 (2026-10-19): 첫 형식
