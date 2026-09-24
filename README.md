# LLM Wiki — Claude Code로 운영하는 개인 지식 위키

자료를 `raw/`에 넣고 "정리해줘"라고 하면, [Claude Code](https://claude.com/claude-code)가 읽고 요약하고 기존 페이지와 연결해 `wiki/`를 계속 키워 가는 Obsidian 볼트 템플릿입니다.

사람은 자료를 넣고 질문만 합니다. 정리, 교차참조, 목차, 일관성 유지는 에이전트가 맡습니다. 에이전트가 규칙을 어기지 않도록 **검사기·리뷰어·hook**으로 이루어진 검증 루프가 작업 종료를 막습니다.

> 이 저장소에는 시스템과 예시 자료 1건만 들어 있습니다. 실제로 쓰는 위키의 자료는 공개하지 않습니다.

## 특징

| | |
|---|---|
| **규칙 문서가 곧 명세** | `CLAUDE.md` 하나에 폴더 구조, 파일명, 페이지 형식, 운영 모드(자료 넣기 · 질문하기 · 건강검진)를 정의 |
| **raw는 불변, wiki는 에이전트 전담** | 원본은 절대 수정하지 않고, 정리본은 에이전트만 씀. 사람이 직접 고친 내용은 존중 |
| **사실과 해석 구분** | 모든 사실 주장에 출처 페이지 링크, 에이전트의 추론은 `> [!note] 해석`으로 표시, 상충하는 정보는 지우지 않고 표시 |
| **핵심 한 줄** | 모든 페이지 맨 앞에 "요약"이 아니라 "내가 가져갈 것" 한 줄 |
| **기계 검사** | `wiki_lint.py`가 형식, 고아 페이지, 빨간 링크, index 누락, 원문에 없는 인용문, log append-only 등을 검사 |
| **의미 검토** | 글을 쓴 에이전트가 아니라 새 문맥의 읽기 전용 서브에이전트(`wiki-reviewer`)가 원문과 대조해 8개 항목 검토 |
| **hook이 강제** | 오류가 남았거나 리뷰어의 "문제 0건" 영수증이 없으면 Stop hook이 작업 종료를 막음 (최대 5회, 넘으면 멈추고 보고) |
| **로컬 git 기록** | 세션 시작 때 사람의 변경을, 검증 통과 시 에이전트의 작업을 따로 커밋해 누가 무엇을 바꿨는지 구분 |

## 동작 흐름

```mermaid
flowchart TD
    A[raw/에 자료 넣기] --> B[Claude Code: 자료 넣기 모드]
    B --> C[요약 페이지 작성]
    C --> D[관련 개념·인물·주제 페이지 생성/수정<br/>양방향 링크, index, log]
    D --> E{wiki_lint.py<br/>오류 0?}
    E -- 아니오 --> D
    E -- 예 --> F{wiki-reviewer<br/>문제 0건?}
    F -- 아니오 --> D
    F -- 예 --> G[Stop hook 통과 → git 커밋]
```

## 구조

```
├── CLAUDE.md                     ← 운영 규칙 (에이전트가 매 세션 읽음)
├── raw/                          ← 원본 자료 (불변, 하위 폴더 없음)
├── wiki/                         ← 정리된 지식 (에이전트가 관리)
│   ├── index.md                  ← 전체 목차
│   └── log.md                    ← 작업 이력 (append-only)
└── .claude/
    ├── wiki_lint.py              ← 규칙 검사기 + hook 로직
    ├── agents/wiki-reviewer.md   ← 의미 검토 서브에이전트
    └── settings.json             ← SessionStart · SubagentStop · Stop hook
```

## 시작하기

필요한 것: [Claude Code](https://claude.com/claude-code), Python 3, (선택) [Obsidian](https://obsidian.md)

```bash
git clone <이 저장소> my-wiki
cd my-wiki
rm raw/2026-09-24_예시-메모-습관.md wiki/요약-메모-습관.md wiki/하루-한-줄-메모.md   # 예시 자료 삭제 (index·log도 비우기)
claude
```

1. Obsidian에서 이 폴더를 볼트로 엽니다 (선택).
2. 자료를 `raw/YYYY-MM-DD_설명.md`로 넣습니다. Obsidian Web Clipper를 쓰면 편합니다.
3. Claude Code에서 "raw에 자료 넣었어"라고 말합니다.
4. 궁금한 것은 그냥 질문합니다. 위키를 먼저 찾아보고 출처 페이지와 함께 답합니다.
5. 가끔 "건강검진"을 요청해 고아 페이지, 빨간 링크, 오래된 정보, 열린 할 일을 점검합니다.

직접 검사기 돌리기: `python3 .claude/wiki_lint.py`

## 내 것으로 바꾸기

- **도메인**: `CLAUDE.md` §3.2의 "현재 도메인" 목록을 자기 관심 분야로 바꿉니다. 검사기가 이 목록을 읽습니다.
- **규칙**: 쓰다가 에이전트가 같은 실수를 반복하면 `CLAUDE.md`에 한 줄을 더하고, 기계로 확인할 수 있으면 검사기에도 추가합니다.
- **다른 폴더에서도 위키 조회**: `~/.claude/CLAUDE.md`에 "정보를 찾을 때는 먼저 이 위키를 조회한다"는 전역 규칙을 두면, 어느 프로젝트에서 작업하든 위키를 참고합니다.

## 영감

- Andrej Karpathy의 "LLM Wiki" 아이디어 — RAG로 매번 원문을 찾는 대신, LLM이 계속 관리하는 누적형 위키
- Geoffrey Huntley의 "Ralph" 루프 — 에이전트가 완료 기준을 통과할 때까지 되돌려 보내는 루프. 이 템플릿에서는 검증 루프 이름으로 씀
