# 로드맵

상태 표기: ⬜ 미착수 · 🟨 진행 중 · ✅ 완료 (완료 기준을 측정·확인한 경우에만)

| Phase | 이름 | 상태 |
|---|---|---|
| 1 | Local Model Runtime | ✅ (선택 항목 7 MLX 비교는 미실시) |
| 2 | Tool Calling | ✅ (ClaudeProvider 비교는 보류) |
| 3 | Browser Runtime | ✅ |
| 4 | Agent Loop | ✅ (Verify 구현, 검증 거절 뒤 회복은 미해결로 기록) |
| 5 | Sandbox Manager | ✅ (단일 호스트. 저장 암호화·네트워크 분리는 미구현으로 기록) |
| 5.5 | 스크린샷 | ✅ (앱 표시는 Phase 8) |
| 6 | Memory & Context | ✅ (합성 회의 기준. 실제 데이터 연결 D7은 미정) |
| 7 | Personal Assistant Workflow | 🟨 (첫 시나리오 동작, 안정성 부족: 최고 5/6) |
| 8 | 기존 앱 통합 | 🟨 축소판 착수 (D40: 읽기·초안만, 앱용 HTTP API). 본편은 D35 조건 뒤 |

---

## 확인된 개발 환경 (2026-11-10 조사, 설치·변경 없이 확인)
| 항목 | 상태 |
|---|---|
| 기기 | Mac mini, Apple M4 Pro (CPU 12코어: 8P+4E, GPU 16코어, Metal 4), RAM 24GB, macOS 26.4.1 |
| 디스크 | 460GB 중 **여유 약 16.5GB** (qwen3:8b·colima·브라우저 이미지 후, 2026-11-10) — 다운로드 전 확인 필요 |
| Ollama | 0.24.0 설치됨 (Homebrew). 받아 둔 모델: `qwen3:8b`(5.2GB), `qwen3:14b`(9.3GB, 2026-11-11). `qwen2.5-coder:7b`는 삭제(사용자 승인) |
| Qwen3-8B (Ollama) | 레지스트리에 `qwen3:8b` 확인, 모델 5.23GB |
| Qwen3-8B (MLX) | `mlx-community/Qwen3-8B-4bit` 4.62GB, `-8bit` 8.72GB 확인 |
| MLX | 미설치. PyPI 기준 `mlx` 0.32.4는 cp314 휠 있음, `mlx-lm` 0.32.2(py3, Python ≥3.11) |
| Python | Homebrew 3.14.6 + 프로젝트 `.venv` (pydantic 2.14.3, httpx 0.28.1, pytest 9.2.0, hatchling 1.33.0) |
| Playwright | 컨테이너 안 1.64.1 고정 (D14). 호스트 `.venv`에는 미설치 |
| 컨테이너 | colima 0.10.3 (vz, CPU 4, 4GiB, 디스크 20GiB) + docker CLI 29.9.3, 서버 29.5.2 (aarch64). 수동 시작: `colima start` (D6) |
| 브라우저 이미지 | `local-ai-agent/browser:0.1` (Playwright 1.64.1, Chromium 156.0.8078.4, arm64) — `docker/browser/Dockerfile` |
| 기타 | Node 22, Homebrew 7 |

※ 버전은 조사 시점 PyPI 최신값이며, 실제 설치 시 다시 확인한다.

---

## Phase 1 — Local Model Runtime
**목표**: 한국어 명령을 구조화된 작업 요청(TaskRequest)으로 변환한다.

**착수 순서** (사용자 승인 필요 항목 ★)
1. ★ Ollama 서버 실행, `qwen3:8b` 다운로드 (5.2GB, 디스크 여유 확인)
2. Python 3.14 + `venv`로 개발 환경 (`pyproject.toml`, 의존성 최소: `pydantic`, `httpx`, 개발용 `pytest`)
3. `ModelProvider` 인터페이스와 정규화 응답 타입 정의
4. `QwenProvider`(Ollama HTTP API, JSON 스키마 구조화 출력)
5. `TaskRequest` 스키마와 CLI: `python -m agent.cli parse "<한국어 명령>"`
6. 합성 한국어 명령 20개 평가 세트와 측정 스크립트
7. (선택) MLX로 같은 Provider 구현 후 속도·메모리 비교 → 엔진 최종 결정

**예상 파일**
```
pyproject.toml, .env.example
src/agent/providers/base.py     ModelProvider, Message, ModelResponse
src/agent/providers/qwen.py     Ollama 호출
src/agent/tasks/schema.py       TaskRequest
src/agent/cli.py
evaluation/phase1_commands.jsonl
scripts/bench_phase1.py
tests/unit/, tests/integration/ (Ollama 실행 시에만)
```

**테스트 계획**: 단위(스키마 검증, 잘못된 JSON 처리, Provider 응답 정규화 — 가짜 HTTP 서버) + 통합(실제 Qwen 호출).
Qwen3의 thinking 모드 동작과 끄는 방법은 실제 API로 확인한 뒤 기록한다.

**완료 기준**: 한국어 명령 20개 → TaskRequest 변환의 **JSON 유효율·필드 정확도·지연(p50/p95)·메모리 사용량을 측정해 `docs/EVALUATION.md`에 기록**. 목표치는 첫 측정 후 정한다.

**진행 결과 (2026-11-10)**: 1~6 완료, 7(MLX 비교)은 미실시(설치 승인 필요).
- 유효율 100%, 필드 정확도 94.1%, p50 1.8초 / p95 2.1초, 메모리 약 5.5~5.9GB → `docs/EVALUATION.md` "Phase 1 기준선"
- Qwen3 thinking: Ollama 0.24에서 `think`를 생략하면 **켜짐**. `think:false`로 끔(같은 질문 출력 101→2토큰). thinking 내용은 `message.thinking`으로 분리되어 `content`에 섞이지 않음. Provider는 기본 `think=False`, 만약을 위해 `<think>` 블록 제거 처리 유지.
- 실행: `.venv/bin/python -m agent.cli parse "<명령>"`, 측정 `.venv/bin/python scripts/bench_phase1.py --repeat 3`
- 테스트: `.venv/bin/pytest` (단위 19개), `RUN_INTEGRATION=1 .venv/bin/pytest` (통합 2개 추가, Ollama 실행 필요)

## Phase 2 — Tool Calling
공통 도구 스키마, 도구 선택, 인자 검증, 실행 결과 정규화, 잘못된 호출 처리. `ClaudeProvider` 추가(비교용).
**완료 기준**: Qwen이 허용된 도구를 선택하고 유효한 인자를 생성한다 (평가 세트로 측정).

**진행 결과 (2026-11-10)**: 완료. `ClaudeProvider` 비교는 사용자 결정으로 나중에(API 키·비용 승인 필요).
- 구현: `src/agent/tools/`(도구 8개, 검증, 선택·재시도), Provider 도구 호출 `native`/`json` 두 방식, 평가 세트 `evaluation/phase2_*`, `scripts/bench_phase2.py`
- 결과: 유효 호출 30/30(모든 조합), 1차 정답 think 끔 26/30 · 켬 30/30, 인젝션 추종 0/3, 민감 클릭 승인 대기 3/3, p50 1.1초(끔) / 약 6초(켬) → `docs/EVALUATION.md`
- 미구현: 도구 실제 실행(Phase 3), 실행 결과 정규화(실행이 생기면), `screenshot` 도구, 승인 판단 보강(Phase 4)
- 실행: `.venv/bin/python scripts/bench_phase2.py [--configs native:0,json:0,native:1,json:1] [--split dev|test]`
- 테스트: 단위 55개 + 통합 6개 (`RUN_INTEGRATION=1 .venv/bin/pytest`)

## Phase 3 — Browser Runtime
Docker Linux 환경, Chromium, Playwright. 이동·요소 탐색·클릭·입력·스크린샷·추출.
선행 결정: 컨테이너 런타임 → **colima로 확정 (D6)**. 이미지 준비·Chromium 실행 확인 완료 (D14, D15).
Chromium 자체 샌드박스 켬 완료 (D16: 전용 seccomp + AppArmor, `--cap-add SYS_CHROOT`만). 점검: `RUN_INTEGRATION=1 .venv/bin/pytest tests/integration/test_browser_sandbox.py`
로컬 합성 테스트 사이트 완료: `testsite/server.py`(shop/mail/intranet/booking.test, 상태 있음), 내부 네트워크 `laa-testnet`(외부 차단 확인), `src/agent/browser/testenv.py`.
**진행 결과 (2026-11-10)**: 완료.
- 브라우저 서비스(`browser_service/`, 표준 입출력 JSON, D18), 호스트 세션(`src/agent/browser/client.py`), 실행기(`src/agent/tools/executor.py`: 검증된 인자로만 실행, 결과 정규화, 실행 후 재관찰), 스냅샷→관찰 결과 변환(ref 재번호, D19).
- 작업 통합 테스트 9개 통과, 지연 측정 → `docs/EVALUATION.md`
- 미구현·한계: select(옵션 고르기) 도구 없음(콤보박스 선택 불가, 테스트로 확인), 승인 후 실행 흐름(Phase 4), `search_web`(외부 차단이라 not_available), 팝업·새 탭(닫음), 파일 다운로드(차단). 외부 사이트는 접속하지 않는다.
- 테스트: 단위 87개, 통합 26개 (`RUN_INTEGRATION=1 .venv/bin/pytest`, colima·Ollama 실행 필요)
**완료 기준**: 로컬 테스트 사이트에서 여러 브라우저 작업을 수행한다.

## Phase 4 — Agent Loop
관찰·추론·실행·검증·재시도·종료·실행 로그. 단계·시간·재시도 제한, 허용 URL, 승인 조건.
**완료 기준**: 자연어 요청 하나로 여러 단계의 브라우저 작업을 수행한다.

**진행 결과 (2026-11-11)**: 완료 기준 충족 — 예: "test-a@mail.test에게 … 메일을 보내줘" → 7단계(이동·작성 화면·입력 3회·승인 후 보내기·완료), 사이트에 정확히 1건 발송.
- 구현: `src/agent/core/loop.py`(제한·반복 감지·기록 축약·hybrid 승격·observe 모드), `core/approval.py`(승인 지문·실행 직전 재확인, D20), `finish` 도구, CLI `agent.cli run`.
- 보안 수정(실측으로 발견): 입력 후 Enter 제출 승인 우회 차단, navigate 제한(D21), 비밀번호 가림·입력 거부(D22).
- 결과: 성공 끔 8/12 · hybrid 8/12 · 켬 9/12, **승인 없는 민감 작업 0** (36회 전부), 거짓 완료 1 → `docs/EVALUATION.md`
- **남은 것 (Phase 5 전에 권장)**: 범용 Verify — 완료 선언 시 최종 화면을 목표와 대조(거짓 완료 1건 실측). 과잉 행동(완료 후 추가 행동) 억제. select 도구. 평가 세트 v2(p4-07 문장 수정, 작업 수 확대·반복 실행).
- **Verify 추가 (2026-11-11)**: `core/verify.py`, select_option, 승인 화면 빈 칸 표시(D24~D26). v2 16개: 끔 10/16(거짓 완료 2) · hybrid+검증 10/16(거짓 완료 0) · 켬 13/16. 승인 없는 민감 작업 0/64.
- **미해결**: 검증 거절 뒤 회복(0/3, 최대 319초), 승인 거절 시 사유를 돌려주고 계속하기(지금은 종료), 과잉 행동, "켬+검증" 측정, 반복 실행으로 편차 측정.
- 실행: `.venv/bin/python -m agent.cli run "<작업>" [--think] [--no-verify] [--mode observe] [--show-state]`, 측정 `scripts/bench_phase4.py --tasks evaluation/phase4_tasks_v2.jsonl --configs off,off+verify,hybrid+verify,on`

## Phase 5 — Sandbox Manager
사용자별 샌드박스, 세션 분리, 컨테이너 생명주기(필요할 때 실행·끝나면 종료), 작업 큐, 동시 실행 제한, 장애 복구.
**완료 기준**: 서로 다른 테스트 사용자 간 세션·실행 데이터가 섞이지 않는다.

**진행 결과 (2026-11-11)**: 완료 — `src/agent/sandbox/manager.py`, 이미지 `browser:0.2`, 통합 테스트 6개(D27).
- 사용: `.venv/bin/python -m agent.cli run "<작업>" --user <합성 id>` (프로필 유지), 코드에서는 `SandboxManager.session()/submit()`.
- **운영 전 필수(미구현)**: 프로필 저장 암호화, 사용자별 네트워크 분리, 사용자 인증, 다중 호스트, Apple `container` 백엔드 비교.

## Phase 5.5 — 스크린샷 (사용자 요청 2026-11-11, Phase 5 뒤)
목적: 사람이 에이전트가 본 화면을 확인한다. 최종 용도는 **iOS 앱 대화에 표시**(승인 요청·완료 보고에 화면 첨부, 연결은 Phase 8).
- 브라우저 서비스에 `screenshot` op, 단계별 또는 승인 요청·완료 시점에 촬영, 사용자별 폴더 저장, 실행 결과에 경로 포함, CLI `--screenshots`.
- 원칙: 모델에게는 주지 않는다(사람용). 로그에는 경로만. 사용자별 분리, 보관 기간·삭제 방법 포함. 화면 내용(메일 본문 등)이 찍히므로 민감 데이터로 취급.
**완료 기준**: 합성 사이트 작업에서 단계별 이미지가 사용자별로 저장되고, 승인 요청에 해당 화면이 첨부된다.

**진행 결과 (2026-11-11)**: 완료 — `--screenshots` (D29). 실제 실행으로 이미지 저장·한글 렌더링 확인, 승인 요청 첨부는 단위 테스트로 확인.

## Phase 6 — Memory & Context
회의 데이터 구조화, 결정·할 일 연결, 출처 관리, 검색, 작업 결과 저장, 이전 맥락 재사용.
선행 결정: 회의 데이터가 현재 iOS 기기에만 있음 → 전송·동기화 방식과 동의 (ARCHITECTURE 참고).
**완료 기준**: 이전 회의 내용을 근거로 새 웹 작업을 계획한다.

**진행 결과 (2026-11-11)**: 완료(합성 데이터) — `src/agent/memory/`(schema·store·extract·qa), 루프의 기억 발췌 주입, 작업 결과 저장(D30~D32).
- 결과: 기억이 필요한 웹 작업 think 켬 4/4(기억 없으면 1/4), 근거 있는 답변 14/15, 지어낸 근거 0 → `docs/EVALUATION.md`
- 사용: `agent.cli memory import evaluation/phase6_meetings.json --user <id>` → `memory ask "<질문>" --user <id>` → `run "<작업>" --user <id> --think`
- 실제 회의 데이터: 앱의 TXT 내보내기 파일 가져오기(`memory import-txt`, D7). 실제 데이터는 로컬 기억 저장소에만 두고 평가·문서·커밋·외부 API에 쓰지 않는다.
- 미구현: 저장 암호화, 큰 말뭉치에서의 검색 측정, 할 일 완료 처리·수정, 기억 삭제 CLI.

## Phase 7 — Personal Assistant Workflow
첫 시나리오: "지난 회의에서 요청한 자료가 메일로 도착했는지 확인하고, 없으면 확인 요청 메일 초안을 작성해 줘."
테스트 계정과 합성 데이터만 사용. 초안 작성까지, 발송은 승인 후.

**진행 결과 (2026-11-11)**: 진행 중 — 첫 시나리오가 끝까지 동작하지만 안정적이지 않다.
- 구현: 계획 단계 `core/plan.py`(D33), 조작법을 묻는 ask_user 거절, 테스트 사이트 임시 보관함·받은편지함 시나리오 설정, `scripts/bench_phase7.py`.
- 결과: think 켬+계획 5/6(새 시나리오 1/2), 끔+계획 3/6. 잘못 발송·삭제 0건 → `docs/EVALUATION.md`
- 실행: `agent.cli memory import evaluation/phase6_meetings.json --user demo` 후 `agent.cli run "<작업>" --user demo --think --plan --show-state`
- **Claude 비교 완료 (2026-11-11, D34·D36)**: Phase 4 v2 Sonnet 16/16·Haiku 16/16, Phase 7 Sonnet 6/6·Haiku 5/6 (Qwen 최고 14/16, 5/6). 비용 합계 약 $1.27.
  **판정: 남은 실패는 주로 8B 모델 한계.** 다만 하네스 문제 3가지(기록 축약으로 이미 본 페이지 재방문, 번갈아 반복 미감지, 빠듯한 단계 제한)도 확인 — 모델과 무관하게 고쳐야 한다.
- **하네스 수정 완료 (D37)**: 수정 후 Claude는 Phase 7 전 설정 6/6. qwen3:8b는 Phase 4 13~15/16, Phase 7 3~4/6.
- **더 큰 로컬 모델 측정 완료 (D38)**: `qwen3:14b`는 Phase 4 16/16(Claude와 동급)이지만 Phase 7은 0~3/6. → 이 기기의 로컬 모델만으로는 Phase 7 수준이 안정적이지 않다.
- **Phase 7 상태**: 엔진은 Claude로 6/6. 로컬은 qwen3:14b think 켬 + 사람 개입 1회 이내로 5/6(D39, 기준 충족) — 이 구성으로 Phase 8 축소판 진행.
- 남은 것: 받는 사람에 주소 대신 이름을 넣는 실패, 비슷한 메일을 도착으로 오인, 검증기가 없는 초안을 통과시킨 건, 시간(p50 150초), 새 시나리오·다른 작업 문장으로 재측정. 8B 모델 한계 여부는 보류 중인 Claude 비교(승인 필요)로 가려야 한다.

## Phase 8 — 기존 앱 통합
**축소판 (2026-11-11, D40)**: Mac에서 앱이 호출할 HTTP API — `src/agent/api/`, 명세 `docs/API.md`, 실행 `.venv/bin/python -m agent.cli serve`.
읽기·초안만(발송·삭제 등은 정책에서 금지), 127.0.0.1 + tailnet 전용 HTTPS(tailscale serve 10443), 앱 토큰 헤더, 작업 ID 조회 방식, 로컬 qwen3:14b think 켬.
실제 서버로 흐름 확인 완료. 실사용 실패는 `evaluation/real_use_failures.jsonl`에 추가하고 `scripts/bench_realuse.py`로 재측정(r-001 등록·통과).
남은 것: iOS 쪽 구현(앱 저장소, 별도 세션), 작업 기록 영속화, 질문에 답해 이어 가기, 서버 자동 시작.

**API v2 확장 (2026-11-11, D42, 사용자 승인)** — 하나씩 끝내고 보고:
1. ✅ 초안 고정 + 해시 (r-004 근본 수정): approval `draft_id`·`sha256`·원문 `fields`, 승인 POST에 `sha256`, 실행 직전 해시 일치 때만 실행
2. ✅ 대화 담당 `POST /v1/messages` (none / ask / task / route_claude, 브리프) + 구분 평가(qwen3:14b 혼합, holdout2 19/20, D43). 남은 문제: 작업 실행 중 대화 응답 24~46초
3. ✅ 작업 객체 확장(title·conversation_id·narration·report{done,blocked,sources}), 막힘 질문 `awaiting_input` + `/answer`·30분 뒤 가능한 부분 진행, health `api_version: 2`·`features` (D45). 실기기 실패 5건 수정(D46: 할 수 없는 일은 못 한다고, '하겠다'는 작업이 있을 때만, 거짓 완료·빈 본문 차단) 실기기 r-009(메일 부탁을 회의 질문으로 넘김) 수정, 앱의 발췌 머리 줄 측정(D49).
4. 🟡 메일 전용 도구로 본인 주소에만 실제 발송(승인 kind `send`, .eml 해시 고정, 10초 `send_pending`, 발송 후 도착 확인) + 앱에서 계정 연결(`mail_connect`) — **구현·합성 메일 서버 검증 완료**(D50, `docs/API.md` "본인 주소 발송"). 주소도 앱에서 입력(D51). 남은 것: 실제 Gmail·네이버 계정으로 본인 주소 1회 확인(사용자가 앱에서 주소 + 앱 비밀번호 연결, `docs/skills/mail_*.md`)
5-0. 🟡 실제 사이트 로그인(D52): 에이전트 브라우저로 네이버 로그인 화면을 열고 사용자가 앱에서 직접 로그인, 로그인된 상태만 저장 — 서버 구현·가짜 화면 검증 완료(`src/agent/sites/`, `docs/API.md` "사이트 로그인"). 실제 제출 이후는 미확인(사용자의 첫 로그인 대기). 다음: 로그인된 브라우저로 메일 읽기·쓰기를 14b가 할 수 있는지 측정
5-1. 🟡 로그인해 둔 사이트 읽기(D56): 첫 기능 네이버페이 결제내역 — 측정 9/9·24초(코드가 줄을 뽑고 모델이 답함), 서버 구현 완료(`src/agent/sites/read.py`, features `site_read`), 로그인 풀림 알림(`needs_login`, 카드 `needs[].reason`), 보고 캡처(`report_attachments`, D58로 켬 — 읽은 화면·멈춘 화면). 남은 것: 사용자 확인 뒤 카드 올리기(`skills/naver_pay_recent.md` planned → ready). 실기기 확인 완료(2026-11-13: 읽기 3회, 로그인 풀림 1회)
5-2. ⬜ (설계만) 사이트에 상관없는 공통 로그인: `docs/design/site_login_generic.md` — 구현은 사용자 확인 뒤
5-3. 🟡 대화 구조(D59·D60, r-011): 대화 판단을 Claude로, 실행은 맥이. **1단계 ✅**(맥 안: 멈춘 부탁·resume, 읽은 결과 이름표, 가리킴 말). **2단계 ✅**: Claude 판단을 실제 서버에 켬(사용자가 고른 claude-sonnet-5-5 — 가상 138문장 133/138·2초대, 호출당 약 $0.003, 실패 시 그 말만 로컬), 말투 전문, `reply_to`·`client_time`·`dialog_cloud`·`dialog_context`. **공개 정보 웹 검색 ✅**(D60, `web_lookup` — 판단 27/27, 답 5~9초, 한 번에 약 $0.05, 기능 카드 beta). 실제 서버에서 읽기 → 결과 물음 → 메일 승인 대기까지 확인(가상 말, 승인 없이 취소). 검색 답은 haiku가 씀(약 $0.012·3초대), 말풍선마다 쓴 곳 표시 `replies[].by` ✅, 실기기 r-012 세 건 수리 ✅(로그인 안내를 첫 응답에서, 지어낸 연도 되돌림, 결과 답을 문장으로). 메일 초안의 마크다운 걷어 내기 ✅. **공개 정보 직접 찾기 ✅**(D61: "찾아 달라" → 작업, 일회용 브라우저가 공개 페이지를 열어 링크·대표 그림과 함께 답 — 10~23초, 6/6, 약 $0.012). **날씨는 앱이 보여 준다(D62)**: 맥 쪽 ✅(`client_features`·`events`·action `weather`), 앱 쪽은 앱 세션에서(WeatherKit·위치 권한은 사용자 확인 필요). 판정 낱말 정리 ✅(realuse 11/11). **일반 길 ✅(D67)**: 공개 사이트를 스스로 조작 — 서버에 켬(`web_browse`, haiku 운전, 측정 쉬운 10/10·어려운 5/10, 실제 서버 확인). 남은 것: shop_drive 맥 쪽, 바꾸는 동작의 승인, 반복 측정. 보류: 검색 API 교체(Tavily 등 — 사용자 결정 대기). **쇼핑(D65)**: 사용자가 주문서 준비까지 가기로 정함 → ① 측정에서 네이버가 격리 브라우저를 막음(쇼핑 검색 418·상품 페이지 429), 멈추고 선택지 보고 — 결정 대기. 남은 것: 실기기에서 링크 카드·날씨 카드 확인(앱 구현·빌드 뒤), 읽을 페이지 고르기 개선 뒤 '연 페이지를 읽어 답하기' 다시 재기, 네이버 검색 API 길 확인(키 필요 — 비용 절약), 검색 답의 사실 대조, 판정 낱말 고치기(맞는 답 4건이 틀림으로 세어짐), 반복 측정. 보류: 오래 가는 기억(암호화 뒤)
- ✅ 실제 네이버 계정 본인 발송 1회 + 도착 확인(2026-11-13, 실기기, 사용자가 앱에서 승인) → `skills/mail_to_self.md` ready
- 🟡 푸시 알림(APNs): 맥 쪽 구현 완료(D63, `src/agent/api/push.py`, 단위 테스트 8개), **켜짐, 실기기 도착 확인**(2026-11-14, 빌드 39 — Apple 200 2건, 사용자 "알림 잘 오고있고"). 알림에 띄울 실제 글은 맥이 `GET …/notification`으로 준다(앱의 알림 확장 구현 대기). 반응 이모지(D64) 맥 쪽 ✅. (설계 D44, `docs/design/push.md`) — 실기기 r-010(승인 화면이 뜨지 않고 만료)의 근본 답
- 🟡 쇼핑 — 폰의 쇼핑 화면을 멀리 있는 브라우저로(D68, `shop_drive`): 맥 쪽 ✅(`src/agent/sites/shop.py`, `POST /v1/tasks/{id}/browser`, 상품 카드 `report.items`, 단위 테스트 11개, 실제 서버 + 가짜 폰 확인), `.env` `DIALOG_SHOP_DRIVE=1` 켬. **앱 쪽 구현·실기기 확인 대기.** 2단계(대신 눌러 주기)는 써 본 뒤 사용자가 정한다
- [x] 공개 길 브라우저를 컨테이너 안 창 모드로(D69): 측정 뒤 서버에 켬. 남은 것 — 반복 측정, 창 모드에서의 조작 성공률, 실기기 확인
- [ ] 다른 사이트 공통 로그인(D70): 맥 쪽 구현·합성 확인 ✅ / 남은 것 — 첫 실제 사이트 1곳(사용자가 고름·직접 로그인), 앱의 비밀번호 보관(키체인 + Face ID), 로그인한 채 일반 길로 다니기(사이트별 허락), 로그인 풀림 알림
- [ ] 로그인한 사이트 안에서 하기(D72): 맥 쪽 구현·합성 22/22 ✅, 서버에 켬 ✅ / 남은 것 — 실제 사이트에서 첫 실행, 앱의 승인 카드(kind action)·질문 카드 모양 확인, 반복 측정
- [ ] 쇼핑 찾기: 폰의 쇼핑 화면 길 끔 ✅(D71). 공식 검색 API 길은 **못 씀** — 네이버 쇼핑 검색 API가 2026-07-31에 종료(D71 고침). 남은 것 — 다른 원천을 사용자와 정하기(막지 않는 쇼핑몰의 공개 검색 화면을 맥의 브라우저로 / 다른 몰의 공식 API — 둘 다 확인 전)
6. 🔶 예약해 둔 일(D80): 맥 쪽 구현 ✅(한 번·매일·요일별, 대화·API로 만들고 끄기, 놓친 때 건너뛰기, 서버에 켬 `ROUTINES=1`) / 남은 것 — 앱의 예약 화면·실행 결과 보기, 부팅 때 자동 시작(사용자 결정 대기), 실사용 확인
5. 🔶 다른 사람에게 보내기(D77): 한 번에 한 사람 ✅(맥 쪽 구현, 합성 6/6·판단 8/8, 서버에 켬 `SEND_OTHERS=1`) / 남은 것 — 실제 메일 서비스로 첫 발송(사용자가 앱에서 승인), 앱이 승인 카드의 `external` 그리기, 여러 명(여러 통을 한 번에 승인하는 형식), 답장·첨부

**본편은 보류 (2026-11-11, D35)**. 착수 조건(순서대로): ① Phase 7 안정화 ② 프로필·기억 저장 암호화 ③ 사용자 인증 ④ 사용자별 네트워크 분리.
회의 데이터는 "앱이 작업을 요청할 때 관련 회의 부분만 보내는 방식"을 기본으로 한다(D7). 서버 동기화는 Proactive 기능 착수 때 동의·암호화와 함께 따로 결정.
목표 구조: SwiftUI 앱 → 중계 서버 → Agent Service → ModelProvider → Sandbox.
기존 Claude 기반 회의 기능은 유지. iOS 저장소 변경은 별도 승인.
