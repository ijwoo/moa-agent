# 에이전트 API 명세 (v1 + v2 확장, Phase 8)

iOS 앱이 Mac의 에이전트에 작업을 맡기고, 진행 상태를 조회하고, 초안을 승인·거부하는 HTTP API.

- **범위**: 읽기, 초안 저장, 승인 뒤 실행. 실행하는 것은 **사용자 본인 주소로 보내는 메일**과, 허락해 둔 경우(`SEND_OTHERS=1`) **정해 둔 사람에게 보내는 메일**, 그리고 사용자가 허락한 사이트 안의 단추 하나다. 삭제·결제·신청처럼 되돌릴 수 없는 동작은 승인 카드 없이는 실행하지 않는다.
- **대상 사이트**: 개발·측정에 쓰는 맥의 합성 사이트(테스트 쇼핑몰·메일·공지·회의실 예약), 사용자가 로그인해 둔 실제 사이트(정해 둔 읽기와 `site_do`), 그리고 공개 사이트(`web_browse`)다.
- **모델**: 대화 판단은 켜져 있으면 Claude가, 사이트에서 읽은 개인 내용은 맥의 로컬 모델이 다룬다. 앱이 붙인 회의 자료의 내용은 `dialog_context`를 켠 경우에만 Claude로 가고, 그 밖에는 붙어 있다는 사실만 간다.
- 구현: `src/agent/api/`. 실행: `.venv/bin/python -m agent.cli serve` (colima, Ollama 필요).

## 접속
| 항목 | 값 |
|---|---|
| 기본 주소 | `https://<tailnet-host>:10443` — tailnet 안에서만 접속된다. 실제 호스트 이름은 `tailscale status`로 확인하고 **저장소에 적지 않는다** |
| 프로토콜 | HTTPS (Tailscale이 인증서 처리), 요청·응답 모두 `application/json; charset=utf-8` |
| 인증 | 모든 요청에 `X-App-Token: <토큰>` 헤더. 토큰은 Mac의 `.env`에 있는 `AGENT_APP_TOKEN` 값(커밋하지 않음). 앱에서는 키체인에 저장 |
| 본문 크기 | 64KB 이하 |
| 버전 선택 | `X-Api-Version: 2` 헤더 (선택, **2 이상의 숫자면 v2** — 나중 버전은 앞 버전을 포함한다). `POST /v1/tasks`·`POST /v1/messages`에 붙이면 그 요청으로 만든 작업이 3단계 질문 방식(`awaiting_input`)을 쓴다. **없으면 v1 방식**(막히면 `needs_input`으로 끝남) — `awaiting_input`을 모르는 배포된 앱이 질문을 실패로 보지 않게 |

`X-Api-Version: 3`(이상)은 2에 더해 **본인 주소 발송**을 켠다: 그 요청으로 만든 작업은 승인 `kind: "send"`와 상태 `send_pending`을 쓸 수 있다. `features`에 `send_self`가 있을 때만 3을 보낸다. 2 이하(또는 없음)면 발송 작업은 만들어지지 않고 지금처럼 초안까지만 한다.

`X-Api-Version: 4`(이상)는 3에 더해 대화에서 **사이트 로그인 화면**(`action.type: "site_login"`)을 받을 수 있다는 뜻이다. `features`에 `site_login`이 있을 때만 보낸다.

서버는 Mac의 `127.0.0.1:8765`에만 열려 있고, `tailscale serve`가 tailnet 전용 HTTPS로 감싼다.

## 작업 상태
```
queued → running ⇄ awaiting_approval → completed
                 ⇄ awaiting_input (v2)
                 awaiting_approval(kind send) → send_pending (v3, 10초) → running(발송·도착 확인) → completed
                 ↘ needs_input | failed | cancelled
```
| status | 뜻 | 앱이 할 일 |
|---|---|---|
| `queued` | 앞 작업을 기다리는 중 (한 번에 하나만 실행) | 계속 조회 |
| `running` | 실행 중. `steps`가 늘어난다 | 계속 조회, 진행 표시 |
| `awaiting_approval` | 초안 저장 승인을 기다림. `approval`에 초안 내용 | 사용자에게 보여 주고 승인/거부 전송 |
| `awaiting_input` (v2, `X-Api-Version: 2`로 만든 작업만) | 작업 담당이 막혀 사용자에게 물음. `question`에 질문·선택지 | 질문을 보여 주고 `POST …/answer`. 30분(`question.expires_in_s`) 안에 답이 없으면 할 수 있는 부분부터 진행하고 `result.report.blocked`에 적는다 |
| `send_pending` (v3, `X-Api-Version: 3`으로 만든 작업만) | 발송을 승인받았고 보내기 전 대기 중(10초). `send`에 보낼 메일과 `cancel_until` | 남은 시간과 취소 버튼을 보여 준다. 이때 `POST …/cancel`이면 보내지 않고 `cancelled`. 대기가 끝나면 `running`으로 돌아가 보내고, 그 뒤 취소는 409 |
| `completed` | 끝. `result.report`(v2)·`result.summary`에 보고 | 결과 표시 |
| `needs_input` | (v1) 정보가 부족해 멈춤. `result.question`·`result.choices`에 질문 | 질문을 보여 주고, 답을 포함해 **새 작업**으로 다시 요청. `X-Api-Version: 2`로 만든 작업은 대신 `awaiting_input`을 쓴다 |
| `failed` | 실패. `result.outcome`에 사유 코드(`site_blocked`: 사이트가 접근을 막아 멈춤 — `report.blocked` 첫 줄에 이유) | 사유 표시. `outcome == "needs_login"`이면 `result.action`(`{"type": "site_login", "site": …}`)으로 다시 로그인 단추를 그릴 수 있다 |
| `cancelled` | 취소됨 | — |

조회 주기: 실행 중에는 1~2초, 승인 대기 중에는 5초 이상을 권장. 승인 대기는 기본 30분 뒤 만료된다(`approval.expires_in_s`).
작업 기록은 서버 메모리에만 있어 **서버를 재시작하면 사라진다**(이후 조회는 404 `task_not_found`).

## 엔드포인트

### `GET /v1/health`
서버 확인. 앱은 **`features`에 있는 기능만 켠다.**
```json
{"ok": true, "scope": "read_draft", "api_version": 2,
 "features": ["messages", "client_message_id", "tone", "hash_approval", "task_questions", "task_report", "narration"]}
```
| feature | 뜻 |
|---|---|
| `messages` | `POST /v1/messages`(대화 담당) |
| `hash_approval` | 승인의 `draft_id`·`sha256`, 409 `hash_mismatch` |
| `tone` | `POST /v1/messages`의 `tone`(friendly / formal) |
| `client_message_id` | `POST /v1/messages`의 `client_message_id`로 재시도해도 같은 응답(작업 중복 없음). 이 값이 있을 때만 자동 재시도를 켠다 |
| `task_questions` | 상태 `awaiting_input`, `task.question`, `POST /v1/tasks/{id}/answer`. 작업을 만드는 요청에 `X-Api-Version: 2`를 붙여야 켜진다 |
| `task_report` | `result.report` = {done, blocked, sources} |
| `narration` | `task.narration` 진행 설명 |
| `send_self` | 본인 주소로 실제 발송(아래 "본인 주소 발송"). 연결된 계정이 없어도 나온다(첫 부탁이 `not_set` 실패로 돌아와 앱이 연결 화면을 띄운다). 작업을 만드는 요청에 `X-Api-Version: 3`을 붙여야 켜진다 |
| `site_login` | 실제 사이트(지금은 네이버) 로그인: `GET /v1/sites`, `POST /v1/sites/{site}/login` 등(아래 "사이트 로그인"). 사용자가 직접 로그인하고 서버에는 로그인된 상태만 남는다 |
| `skills` | 할 수 있는 일 카드: `GET /v1/skills`(아래). 카드의 `prompt`를 대화에 보내면 그 일이 시작된다 |
| `site_read` | 로그인해 둔 사이트의 정해진 페이지를 열어 읽고 답하는 작업(아래 "사이트 읽기"). 로그인이 없거나 풀렸으면 `outcome: "needs_login"` |
| `reply_to` | `POST /v1/messages`의 선택 키 `reply_to {task_id}`를 받는다 (D59) |
| `dialog_cloud` · `client_time` | 대화 판단을 Claude가 한다 / 선택 키 `client_time`을 쓴다. 서버에서 켰을 때만 실린다 (D59, "대화 판단이 어디서 되나") |
| `dialog_context` | `dialog_cloud`일 때, 앱이 붙인 회의 자료(`context`)의 **내용**이 Claude로 간다. `dialog_cloud`는 있는데 이것이 없으면 "붙어 있다"는 사실만 간다 |
| `reply_by` | 응답의 `replies[].by` — 그 말풍선을 쓴 곳 `claude` \| `local` \| `mac` ("대화 판단이 어디서 되나") |
| `push_text` | 알림에 띄울 실제 글을 `GET /v1/tasks/{id}/notification`으로 준다(`push`와 함께 실린다) |
| `push` | 푸시 알림: 맥에 APNs 키가 설정돼 있을 때만 실린다. 앱은 이것이 있을 때만 기기를 등록한다 (D44, "푸시 알림") |
| `reaction` | 요청에 `client_features: ["reaction"]`이 있으면 응답에 선택 키 `reaction`(이모지 하나)이 실릴 수 있다. `dialog_cloud`일 때만 (D64, "반응 이모지") |
| `voice_call` | health `features`에 있을 때만(대화 판단이 Claude일 때) 앱이 전화 단추를 보인다. 통화 중인 말이면 요청 `client_features`에 `"voice_call"`을 넣는다(D116). 답은 귀로 듣는 말투(말풍선 1~2개, 짧은 문장, 목록·이모지·링크 없음)가 되고, 말풍선마다 소리로 읽을 글 `replies[].speak`가 붙는다. 승인은 목소리로 받지 않는다("화면의 승인 카드를 눌러 주세요"). 끝난 작업에는 늘 `result.report.speak`(한두 문장)가 있다 |
| `voice_tts` | 통화 목소리(D117): 사용자의 목소리 샘플로 맥이 만든 음성. `voice_call`인 말의 말풍선에 `voice_id`가 붙는다(이미 만들기 시작함). 통화를 걸 때 `POST /v1/voice/warm`, 다른 글(예: `report.speak`)은 `POST /v1/voice {"text"}` → `{"id"}`. 조각은 `GET /v1/voice/{id}/{n}`(n=0부터)로 받는다. 응답: 200 `audio/wav`(24kHz 모노 16bit, 약 1초), 204(끝, 더 없음), 409 `not_ready`(10초 기다려도 아직 — 같은 주소로 다시), 503 `voice_unavailable`(폰 목소리로 읽기), 404(없거나 10분 지남). 끼어들면 `DELETE /v1/voice/{id}` |
| `shop_view` | 요청에 `client_features: ["shop"]`이 있으면 살 것을 찾는 말에 `action.type: "shop"`을 돌려주고, 요청의 `view`(앱이 화면에서 읽은 상품 칸)를 받는다. `dialog_cloud`일 때만 (D66, "쇼핑 — 옆에서 봐 주기") |
| `site_do` | 로그인해 둔 사이트 안에서 하는 일이 **작업**이 된다(`skill: "site_do"`, D72) — 작업 안의 질문 카드(이 사이트의 화면을 Claude가 봐도 되는지)와 승인 카드(`kind: "action"`)를 쓴다. 아래 "사이트 안에서 하기" |
| `shop_api` | 살 것을 찾는 말이 **작업**이 되어(`skill: "shop_api"`) 공식 쇼핑 검색 API로 찾은 상품을 `result.report.items` 카드로 준다(D71). 화면을 열지 않는다 — `browser`는 오지 않는다. 앱의 `client_features`와 상관없이 된다 |
| `shop_drive` | 요청에 `client_features: ["shop", "shop_drive"]`가 있으면 살 것을 찾는 말이 **작업**이 된다 — 작업 상태의 `browser`(폰이 할 일)와 `POST /v1/tasks/{id}/browser`. 서버에서 켰을 때만 (D68, "쇼핑 — 찾아 와서 견줘 주기") |
| `weather_action` | 요청에 `client_features: ["weather"]`가 있으면 날씨 물음에 `action.type: "weather"`를 돌려준다. `dialog_cloud`일 때만 (D62, "날씨는 앱이 보여 준다") |
| `web_find_reads_cloud` | (기본은 없음) 직접 찾기에서 맥의 브라우저가 연 공개 페이지의 글을 Claude가 읽는 설정일 때만 실린다 |
| `web_browse` | 정해진 기능에 없는 부탁을 맥의 브라우저가 공개 사이트에서 직접 해 본다(일반 길). 서버에서 켰을 때만 (D67, "사이트를 직접 열어 보기") |
| `web_find` · `report_links` | "찾아 달라"는 말은 맥의 브라우저가 공개 페이지를 직접 여는 작업이 된다 / 그 결과에 `result.report.links`가 실린다. 서버에서 켰을 때만 (D61, "공개 정보 직접 찾기"). 후보 페이지를 네이버 검색 API로 찾을 때는 `web_find_naver`도 실린다 |
| `web_lookup` | 공개 정보(날씨·가격·운영 시간·뉴스·환율)를 웹 검색으로 찾아 답한다. `dialog_cloud`일 때만, 서버에서 켰을 때만 실린다 (D60, "공개 정보 웹 검색") |
| `site_login_resume` | 로그인이 없어 못 한 부탁의 안내에 `action.resume: true`가 실린다. 로그인 뒤 그 말을 다시 보내면(또는 사용자가 "했어"라고 하면) 이어서 한다 (D59) |
| `report_attachments` | 보고의 화면 캡처 `result.report.attachments`(아래). 서버에서 켰을 때만 나온다(실제 서버는 2026-11-12부터 켬) |
| `mail_read` | 오늘 받은 메일 읽기: `GET /v1/mail/today`, `POST /v1/mail/briefing`. 대화에서 "오늘 온 메일 요약해줘"에 바로 답한다 |
| `mail_connect` | `GET /v1/mail/accounts`, `POST /v1/mail/accounts/check`·`connect`·`disconnect`. **이 값이 없으면 앱은 비밀번호를 보내지 않는다** |
| `push` (예정) | 푸시 알림 — 구현되고 APNs 키가 설정되면 추가 (`docs/design/push.md`) |
`features`가 없으면(이전 서버) 모두 꺼진 것으로 본다.

### `GET /v1/skills` — 할 수 있는 일 (`skills`, D55)
에이전트가 지금 할 수 있는 일의 목록. 서버의 기능 파일(`skills/*.md`, 기준은 `skills/README.md`)에서 만든다. 되는 것이 확인된 일만 올라오고, 하나씩 늘어난다.
```json
{"skills": [
  {"id": "mail_briefing", "title": "오늘 온 메일 브리핑", "description": "오늘 받은 메일을 읽고, 챙겨야 할 것부터 정리해 드려요. 읽음 표시는 바꾸지 않아요.",
   "category": "메일", "prompt": "오늘 온 메일 요약해줘", "status": "ready", "available": true, "needs": []},
  {"id": "naver_mail_connect", "title": "네이버 메일 연결", "description": "…", "category": "연결", "prompt": "네이버 메일 연결해줘",
   "status": "ready", "available": false, "needs": [{"kind": "site:naver", "message": "네이버 로그인이 필요해요", "prompt": "네이버 로그인해줘"}]}
]}
```
- 순서는 서버가 정한 그대로. `category`로 묶어 그릴 수 있다. `status`: `ready`(확인됨) | `beta`(일부만 확인 — 표시해 준다).
- **부탁하기**: 카드의 `prompt`를 사용자의 말로 `POST /v1/messages`에 보낸다(새 형식 없음). 응답은 평소 대화와 같다(답, 작업, `site_login` 등).
- `available: false`면 `needs`의 `message`를 보여 주고, 그 `prompt`를 보내는 단추를 둔다(예: "네이버 로그인해줘"). 준비가 끝나면 다시 조회한다.
  `needs[].reason`: `"never"`(한 적 없음) | `"expired"`(쓰다가 로그인이 풀린 것을 알았다 — `message`가 "네이버 로그인이 풀렸어요. 다시 로그인해 주세요"로 바뀐다).
- `site_login` action을 받으려면 `X-Api-Version: 4`가 필요하다(네이버 로그인·네이버 메일 연결 카드).

### `POST /v1/messages` — 대화 (v2, D42)
앱의 **모든 메시지를 이 하나로 보낸다.** 맥의 대화 담당이 메시지마다 네 가지 중 하나를 고른다.

요청:
```json
{
  "text": "김팀장에게 매출 자료 요청하는 메일 초안 써줘",
  "conversation_id": "c_x8Kq2LmN0pQr7sTu",
  "context": "(선택) 앱이 고른 관련 회의 발췌",
  "contacts": {"김팀장": "kim@example.com", "이대리": "lee@example.com"}
}
```
| 필드 | 형식 | 설명 |
|---|---|---|
| `text` | 문자열 1~16000자, 필수 | 사용자 메시지 (2026-11-17 2000 → 16000: 제작안·대본을 통째로 붙여 보내는 일) |
| `conversation_id` | 문자열, 선택 | 처음엔 생략하고, 응답의 값을 다음 메시지부터 보낸다. 서버가 모르는 id(재시작 등)면 새 대화를 만들고 `conversation_reset: true` |
| `context` | 문자열 8000자 이하, 선택 | 앱이 고른 회의 발췌. 작업을 만들면 작업 담당에게 넘긴다. 저장·응답 반환하지 않는다. 앱은 맨 앞에 머리 줄("오늘 날짜: …", "부탁이 가리키는 날: …")을 붙일 수 있다(측정: 판단 그대로). **머리 줄에 `그 날 녹음한 회의는 없습니다`라는 글자가 있으면** 서버는 메일 부탁에 "그 날 녹음한 회의가 없어요…"라고 되묻는 데 쓴다 — 이 문구는 앱과 맞춘 약속이라 바꾸려면 서버도 함께 바꾼다 |
| `client_message_id` | 문자열 8~64자(영문·숫자·`-`·`_`, UUID 권장), 선택 | 메시지마다 앱이 새로 만든다. **같은 값으로 다시 보내면 서버는 새로 판단하지 않고 처음 응답(같은 작업)을 돌려준다**(`replayed: true`, 작업은 최신 상태). 첫 요청이 아직 처리 중이면 끝날 때까지 기다렸다 같은 응답. 첫 요청이 오류(502 등)로 끝났으면 기억하지 않으므로 다시 처리된다. 24시간 기억. → 응답이 끊겼을 때 "다시 시도"를 같은 값으로 보내면 작업이 두 번 생기지 않는다 |
| `tone` | `"friendly"` \| `"formal"`, 선택 | 채팅 답(`replies[].text`)의 말투. friendly=친근한 해요체, formal=합니다체. 판단(무엇을 작업으로 만들지)·브리프·작업·초안·승인·narration·report는 바뀌지 않는다. 없으면 지금과 같다. `health.features`에 `"tone"`이 있을 때만 보낸다 |
| `contacts` | `{이름: 메일 주소}` 객체 50개 이하, 선택 | **사용자가 확인한** 연락처. 보내면 그 대화의 연락처를 이 값으로 바꾼다(생략하면 이전 값 유지). 메일 주소는 여기 있거나 사용자가 메시지에 직접 쓴 것만 쓴다 |

응답 `200`:
```json
{
  "conversation_id": "c_x8Kq2LmN0pQr7sTu",
  "replies": [{"text": "네, 매출 자료 요청 메일 초안 준비할게요."}],
  "action": {
    "type": "task",
    "task": { "id": "Zk3v9Qw1xY7aBcD2", "status": "queued", "title": "매출 자료 요청 메일 초안",
              "brief": {"purpose": "…", "targets": ["kim@example.com"], "instructions": "…"}, "…": "작업 객체" }
  }
}
```
| `action.type` | 뜻 | `replies` | 앱이 할 일 |
|---|---|---|---|
| `none` | 일상 대화 | 답 1개 | 그대로 표시. 브라우저·작업 없음 |
| `ask` | 정보 부족 | 질문 1개 | 질문 표시. `action.choices`(2~4개, 있을 때만)를 버튼으로. 사용자가 고른 값을 **같은 `conversation_id`로** 다음 메시지로 보낸다 |
| `task` | 작업 생성 | 확인 문장 1개("네, ○○ 준비할게요.") | 확인 문장을 먼저 보여 주고, `action.task.id`로 `GET /v1/tasks/{id}` 조회 시작(승인 흐름은 v1과 같다) |
| `route_claude` | 회의 내용 질문 | 빈 배열 | 맥은 답하지 않는다. 앱이 **기존 Claude 경로**로 같은 질문을 처리한다 |

- 작업 객체에는 v1 필드에 더해 `title`과 `brief`(`purpose`, `targets`, `instructions`)가 들어간다. `task`는 작업 담당에게 넘긴 문장(브리프를 펼친 것)이다.
- 안전장치: 모델이 사용자가 주지 않은 메일 주소로 작업을 만들려 하거나, 받는 사람을 모르는 메일 작성 작업을 만들려 하면 서버가 작업을 만들지 않고 `ask`로 바꾼다(주소 추측 금지).
- 할 수 있는 일(D46): 지금은 맥의 테스트 사이트(mail.test·intranet.test·shop.test·booking.test)만 다룬다. 네이버·지메일 같은 실제 서비스는 "할 수 있다"고 답하지 않고, 그런 작업은 만들지 않는다(`none` + "아직 다루지 못해요" 답).
- **메일 부탁은 회의 답변 경로로 넘기지 않는다(D49).** "…메일로 보내줘"·"메일 써 줘" 같은 부탁(또는 서버의 메일 되물음에 대한 답)인데 판단이 `route_claude`이거나 작업 없는 약속이면, 서버가 `ask`로 필요한 것을 묻는다: 발췌 머리 줄에 `그 날 녹음한 회의는 없습니다`가 있으면 "그 날 녹음한 회의가 없어요. 다른 날 회의 내용으로 보낼까요? 보낼 회의나 내용을 알려 주세요.", 받는 주소를 모르면 "메일을 받을 주소를 알려 주세요.", 그 밖에는 "어떤 내용을 담아 보낼지 알려 주세요." 형식은 기존 `ask`와 같다(선택지 없음). 사용자가 답하면 같은 `conversation_id`로 보내면 된다.
- **"하겠다"는 답 = 작업이 있다(D46).** `none`/`ask`의 답에 "작성할게요"·"확인해 드릴게요" 같은 약속이 있으면 서버가 다시 판단하고, 그래도 작업이 없으면 약속 대신 "어떤 작업을 해 드릴지 한 번만 더 구체적으로 알려 주시겠어요?"(`ask`)로 바꾼다. 앱은 `action.type`이 `task`일 때만 진행 표시를 하면 된다.
- 대화 기록(최근 12턴)과 연락처는 서버 메모리에만 있고 24시간 뒤 또는 재시작 때 사라진다. 로그에 쓰지 않는다.
- 응답 시간(실측): 일상 대화·회의 질문 2~3초, 작업을 만들거나 되묻는 경우 20~30초(한 번 더 신중히 판단한다). **작업이 실행 중이면 같은 모델을 차례로 쓰므로 일상 대화도 20~50초 걸릴 수 있다** — 앱은 요청 시간 제한을 120초 이상으로 두고 "생각 중" 표시를 권장.
- `replies`와 `choices`는 항상 문자열이다.
- 오류: 400 `invalid_request`(text·contacts·conversation_id 형식), 429 `busy`(task인데 대기열이 참), 502 `model_error`(모델 호출 실패).

### 사진·PDF 붙여 묻기 (`uploads`, D73)
`features`에 `uploads`가 있을 때만. 사용자가 대화에 사진·PDF를 붙이면 Claude가 직접 보고 답한다(사용자 결정 2026-11-15 — 로컬 모델은 글만 읽는다).

**1) 올리기** — `POST /v1/uploads`
- 본문은 **파일 그대로**(JSON·multipart 아님). `Content-Length` 필수. 헤더 `X-Filename`(선택): 파일 이름을 퍼센트 인코딩해서(보여 주는 이름일 뿐이다).
- 받는 것: 사진 JPEG·PNG·GIF·WebP(**7MB 이하**), PDF(**10MB 이하**). 종류는 앱이 보낸 `Content-Type`이 아니라 **파일 머리의 글자**로 맥이 정한다. 영상·HEIC·문서 파일(docx 등)은 받지 않는다 — **HEIC는 앱이 JPEG로 바꿔서 올린다.**
- 권장: 사진은 긴 변 2,000px 안팎의 JPEG로 줄여서 올린다(더 커도 Claude가 줄여서 본다 — 글씨가 작은 문서 사진은 너무 줄이지 않는다).
- 응답 `201 {"upload": {"id": "u_…", "kind": "image"|"pdf", "mime", "bytes", "name", "expires_in_s"}}`.
- 오류: `415 unsupported_media_type`(받지 않는 종류), `413 payload_too_large`(한도 초과 — 이때 서버가 연결을 끊는다), `400 invalid_request`(빈 본문), `404 not_found`(기능이 꺼져 있다).
- 맥의 **메모리에만** 둔다(디스크·기록에 쓰지 않는다). 30분 동안 쓰이지 않으면 지운다(`expires_in_s`). 서버를 다시 띄우면 사라진다.

**2) 말에 붙이기** — `POST /v1/messages`의 선택 키 `uploads: ["u_…", …]`
- 한 말에 4개까지(넘는 것은 버린다). 붙인 것이 있으면 `text`는 빈 문자열이어도 된다(말 없이 사진만 보내기).
- 응답은 보통의 대화 응답이다: 보고 답하면 되는 말은 `action: none` + `replies`(by `claude`). 붙인 내용으로 다른 일을 시키면(예: "이 일정 ○○에 넣어줘") 그 일의 작업이 만들어진다 — 필요한 값은 Claude가 사진에서 읽어 글로 넘긴다.
- **이어지는 말**: 같은 대화의 다음 5마디까지는 `uploads`를 다시 보내지 않아도 맥이 그 사진·PDF를 다시 보여 준다("합계가 얼마야?" → "제일 비싼 건?"). 그 뒤에는 다시 붙여야 한다. 새로 붙이면 앞의 것은 내려간다.
- 못 보는 경우(모두 `action: none`, by `mac`): id가 지워졌거나 모르는 것 → "붙이신 것을 받지 못했어요(시간이 지나 지워졌을 수 있어요). 다시 붙여 주세요." / Claude에 연결하지 못함 → "지금은 붙이신 것을 볼 수 없어요…"
- 아직 안 되는 것: 붙인 파일 자체를 메일에 첨부해 보내기, 사이트에 올리기(그렇게 말한다). 영상.
- 앱이 그릴 것: 사용자 말풍선에 붙인 사진의 썸네일(앱이 가진 원본으로 — 맥은 올린 것을 돌려주지 않는다), 올리는 동안의 진행 표시, 실패 안내.

### `POST /v1/tasks` — 작업 요청 (v1, 대화 없이 바로 작업)
요청:
```json
{
  "task": "지난 회의에서 요청한 자료가 메일로 도착했는지 확인하고, 없으면 확인 요청 메일 초안을 작성해 줘.",
  "context": "(선택) 이 작업에 필요한 회의 발췌 텍스트",
  "plan": false
}
```
| 필드 | 형식 | 설명 |
|---|---|---|
| `task` | 문자열, 1~2000자, 필수 | 한국어 요청 |
| `context` | 문자열, 8000자 이하, 선택 | 앱이 고른 관련 회의 부분. 이 작업에만 쓰이고 서버에 저장되지 않으며 조회 응답에도 돌려주지 않는다 |
| `plan` | 불리언, 선택(기본 false) | true면 실행 전에 계획을 세우고 `plan`에 보여 준다(느려진다) |

응답 `202`: 작업 객체(아래). `status`는 `queued` 또는 `running`.

### `GET /v1/tasks/{id}` — 상태 조회
응답 `200`: 작업 객체.
```json
{
  "id": "Zk3v9Qw1xY7aBcD2",
  "status": "awaiting_approval",
  "task": "…",
  "created_at": "2026-11-11T03:10:00+00:00",
  "updated_at": "2026-11-11T03:10:42+00:00",
  "plan": ["받은편지함을 연다", "…"],
  "steps": [
    {"n": 1, "tool": "navigate", "args": {"url": "http://mail.test/inbox"}, "status": "ok", "url": "http://mail.test/inbox"},
    {"n": 2, "tool": "type", "args": {"ref": "e3", "text": "test-b@mail.test", "submit": false}, "status": "ok", "url": "http://mail.test/compose"}
  ],
  "approval": {
    "approval_id": "pJ2mX9aQ",
    "kind": "draft",
    "action": "임시 저장",
    "url": "http://mail.test/compose",
    "draft_id": "d_Qm3xK8pLz2Ab",
    "sha256": "3f1c…(16진수 64자)",
    "fields": {"받는 사람": "test-b@mail.test", "제목": "매출 자료 확인 요청", "본문": "안녕하세요.\n\n지난 회의에서 …"},
    "requested_at": "2026-11-11T03:10:42+00:00",
    "expires_in_s": 1800
  },
  "result": {"outcome": "completed", "summary": "…"}
}
```
| 필드 | 있는 때 | 설명 |
|---|---|---|
| `title` | 항상(v2) | 짧은 제목. 대화로 만든 작업은 대화 담당이 정하고, `/v1/tasks`로 만든 작업은 요청 앞 30자 |
| `brief` | 대화로 만든 작업 | `{purpose, targets, instructions}` |
| `conversation_id` | 대화로 만든 작업 | 만든 대화의 id |
| `narration` | 항상(v2, 처음엔 빈 배열) | `[{"time", "text"}]` 사람이 읽는 진행 설명. 예: "테스트 메일을 열었어요", "'본문' 칸을 채웠어요", "초안을 저장해도 될지 승인을 기다리고 있어요". 입력한 내용(초안 본문 등)은 넣지 않는다. 같은 문장은 연달아 넣지 않는다 |
| `question` | `status == "awaiting_input"`일 때만 | `{"question_id", "text", "choices": [0~4개], "asked_at", "expires_in_s", "received"?}` |
| `plan` | `plan: true`로 요청했을 때 | 문자열 배열 |
| `browser` | `status == "running"`이고 폰이 할 일이 있을 때만 (`shop_drive`) | `{"id": "b1", "type": "search", "query": "…"}` 또는 `{"id": "b2", "type": "open", "items": [0, 2]}` — 아래 "쇼핑 — 찾아 와서 견줘 주기" |
| `skill` | 정해진 길의 작업일 때만 | 어느 길인지: `web_find` \| `web_browse` \| `shop_drive` \| 사이트 읽기 기능의 이름(`GET /v1/skills`의 이름과 같다). 메일 쓰기 같은 일반 작업에는 이 키가 없다. 앱은 이 값으로 그 작업에 맞는 표시를 고를 수 있다(예: 조회 작업에는 '저장된 초안이 없다' 줄을 붙이지 않는다) |
| `steps[]` | 항상(처음엔 빈 배열) | `tool`: navigate·observe·click·type·select_option·scroll·extract_text·ask_user·finish. `status`: ok·error·rejected·policy_denied·needs_approval·approval_feedback 등. `args`의 문자열은 80자로 잘린다 |
| `approval` | `status == "awaiting_approval"`일 때만 (`draft_id`·`sha256`·`received`는 아래 "초안 고정과 해시" 참고) | `fields`는 저장하려는 초안의 입력칸 값(빈 칸은 빈 문자열). 브라우저에서 읽은 **원문**이라 길이 제한이 없고 줄바꿈(`\n`)이 들어 있다 — 자르거나 줄바꿈을 합치지 말고 그대로 보여 준다(스크롤 가능하게). 칸 이름(키)은 겹치지 않는다: 화면에 이름표가 없는 칸은 그 칸 앞의 글이나 종류("날짜"…)로, 그것도 없으면 `"입력 칸"`, 겹치면 `"입력 칸 (2)"`처럼 온다(2026-11-15부터 — 전에는 이름 없는 칸이 빈 키로 겹쳐 값이 사라질 수 있었다). 형식은 그대로 |
| `result` | 종료 상태일 때 | `outcome`, `summary`, `approved_drafts`, `report`(v2), (`needs_input`이면) `question`·`choices` |

`result.report` (v2): **결과 화면은 `summary` 대신 이것과 `approved_drafts`로 그린다.**
```json
{"done": ["test-a@mail.test에게 보낼 회의록 초안을 임시 저장"], "blocked": ["답을 받지 못한 질문: 어느 분기 자료인가요?"],
 "sources": ["http://mail.test/compose", "http://mail.test/drafts"]}
```
- `done`: 한 일(작업 담당 보고, 없으면 `[summary]`). 초안 본문은 다시 쓰지 않는다 — 초안은 `approved_drafts`로 보여 준다.
- `blocked`: 못 한 일과 이유. 시간 안에 답을 못 받은 질문은 서버가 덧붙인다.
- `outcome`이 `verify_failed`면 `done`은 빈 목록이다(검증에서 거절된 완료 보고는 확인되지 않은 주장이라 보여 주지 않는다). 거절 이유는 `blocked` 첫 줄 "완료 보고가 검증을 통과하지 못함: …".
- `task.question.question_id`는 질문마다 새 값이다. 답한 뒤 같은 작업이 다시 `awaiting_input`이 되면 새 `question_id`가 온다.
- `sources`: 실제로 연 페이지 URL(단계 기록에서 서버가 뽑음, 모델이 쓴 것 아님), 최대 10개.
- `attachments` (2026-11-12 — 서버를 `REPORT_ATTACHMENTS=1`로 띄워 `health.features`에 `"report_attachments"`가 있을 때만 온다. **지금 실제 서버는 켜져 있다**): 작업이 읽은 화면, 또는 멈춘 화면의 캡처. 없으면 키 자체가 없다.
  - **언제 무엇이 붙나** (서버가 상황에 맞게 고른다. 작업 하나에 2장까지 — 지금은 모두 1장):
    | 작업이 끝난 모양 | 붙는 화면 | `sensitive` |
    |---|---|---|
    | 사이트 읽기(`site_read`)가 `completed` | 답을 읽은 그 화면 | `true` |
    | 사이트 읽기가 `needs_login`/`login_expired`로 멈춤 | 멈춘 화면(사이트가 돌려보낸 로그인 화면) | `true` — 보관한 로그인 상태를 실은 브라우저의 화면이라 기억된 아이디가 보일 수 있다 |
    | 사이트 읽기가 `site_blocked`(사이트가 막음)·엉뚱한 화면·답 만들기 실패로 멈춤 | 멈춘 그 화면 | `true` |
    | 합성 사이트(mail.test 등) 읽기 작업이 `completed`(저장·발송한 초안이 없을 때) | 마지막으로 본 화면 | `false`(로그인이 없는 화면) |
    | 합성 사이트 작업이 막혀서 멈춤(`verify_failed`·`max_steps`·`timeout`·`too_many_failures`·`loop_detected`) | 멈춘 화면 | `false` |
    | `cancelled`, 승인 거절·만료, `needs_input`, `send_failed`, `needs_login`/`login_missing`(연 화면이 없음), 초안 저장·발송 작업 | 붙지 않는다 | — |
  - `caption` 예: "네이버페이 결제내역 화면", "네이버 로그인 화면(로그인이 풀려 여기서 멈춤)", "테스트 메일 화면(여기서 멈춤)". 모두 코드에 적힌 글이다.
  ```json
  "attachments": [{"id": "a1", "type": "screenshot", "mime": "image/jpeg", "width": 1280, "height": 800, "bytes": 183520,
                   "page_host": "pay.naver.com", "caption": "네이버페이 결제내역 화면", "sensitive": true,
                   "expires_at": "2026-11-13T12:30:00+00:00", "path": "/v1/tasks/{id}/attachments/a1"}]
  ```
  - **받는 방법**: 그림은 작업 객체에 넣지 않는다. `GET {path}`(같은 `X-App-Token`) → `200`, `Content-Type: image/jpeg`, 본문이 그림 바이트.
    응답에 `Cache-Control: no-store`가 붙는다.
    지워졌거나 기한이 지나면 `404` `attachment_gone`(작업 자체가 없으면 `404` `task_not_found`). 목록(`GET /v1/tasks`)에는 `attachments`가 없다.
    `id`는 영문·숫자·`_`·`-`만, 80자 이하다 — 앱은 `path`를 그대로 쓰지 않고 `v1/tasks/{작업 id}/attachments/{id}`를 직접 조립해도 된다(`path`는 참고용).
  - **크기 제한**: JPEG, 가로 1280px 이하(보이는 화면만, 긴 페이지 전체가 아님), 한 장 400KB 이하, 작업 하나에 2장까지. `width`·`height`·`bytes`는 실제 값.
  - `page_host`는 호스트만(주소 전체는 주지 않는다 — 주소에 개인 값이 들어갈 수 있다). `caption`은 **모델이 아니라 서버 코드가 붙인** 짧은 설명이다(60자 이하, 화면 내용을 옮기지 않는다).
  - **개인 정보**: 로그인한 사이트의 캡처는 사용자 본인의 실제 화면이다(`sensitive: true`). `sensitive`가 없으면 `true`로 본다 — `false`는 로그인하지 않은 공개 페이지일 때만 준다. 서버는 캡처를 **메모리에만** 두고(파일·기록·모델 입력에 넣지 않는다)
    `expires_at`(만든 뒤 24시간)이 지나거나 서버를 다시 띄우면 사라진다. 그 사이트를 `logout`하면 그 사이트의 캡처도 지운다.
    앱은 `sensitive` 캡처를 ①클라우드 경로(Claude로 가는 요청)·공유·사진첩에 자동으로 넣지 않고 ②기기에 오래 보관하지 않으며(화면에 보일 때 받아 쓰고, `expires_at` 뒤에는 지운다) ③처음에는 가려 두고 누르면 보이게 하는 것을 권한다.
  - 앱은 모르는 `type`은 무시한다. 캡처가 없어도 보고(`done`·`blocked`·`sources`)는 그대로다.

`result.approved_drafts`: 사용자가 승인해 저장된 초안(또는 보낸 메일) 목록 `[{"draft_id", "sha256", "kind", "action", "url", "fields"}]`. `kind`는 `"draft"`(저장) 또는 `"send"`(발송, v3) — 없으면 draft로 본다. `fields`는 승인 카드와 같은 원문(줄바꿈 포함), `sha256`은 승인한 해시다.

### 초안 고정과 해시 (D42)
승인 요청이 만들어지는 순간 서버는 초안을 **원문 그대로 고정**한다(브라우저 입력칸의 실제 값, 자르기·공백 합치기 없음).
`approval.draft_id`는 고정한 초안의 id, `approval.sha256`은 그 해시다. 계산식(앱에서 같은 값을 재현할 수 있다):

```
sha256( UTF-8( JSON{"action": approval.action, "fields": approval.fields, "kind": approval.kind, "url": approval.url} ) )
JSON 직렬화: 키 사전순 정렬, 구분자 "," 와 ":" (공백 없음), 비ASCII 문자는 이스케이프하지 않음, 값은 원문 그대로(줄바꿈 \n 포함)
```
Python으로는 `json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`와 같다.
승인 후 실행 직전에 서버가 화면의 입력칸 원문을 다시 읽어 같은 식으로 계산하고, **승인한 해시와 같을 때만** 저장한다. 다르면 저장하지 않고 `approval_mismatch`로 끝난다. **저장된 내용은 `summary`가 아니라 이 값으로 표시한다** — `summary`는 모델이 쓴 문장이라 실제 저장 내용과 다를 수 있다(실사용에서 확인됨).

`result.outcome` 값: `completed`, `needs_user`(→ status `needs_input`), `cancelled`, `approval_denied`(사유 없이 거부했거나 2번 고쳐도 승인되지 않음), `approval_expired`, `approval_mismatch`(승인한 내용과 실행 직전 화면이 달라 저장하지 않음, 또는 승인 화면에 보여 줄 입력칸 원문을 읽지 못해 승인을 요청하지 않음), `max_steps`, `timeout`, `too_many_failures`, `loop_detected`, `verify_failed`(완료 보고가 검증을 2번 통과하지 못함 — 예: 저장 버튼을 누르지 않고 '저장했다'고 보고), `send_failed`(v3: 본인 주소 발송을 하지 못함, `result.reason`에 종류, 보낸 메일 없음), `provider_error`(모델 호출 실패), `internal_error`, `site_blocked`(사이트가 막아 멈춤), `needs_login`, `browser_failed`(`shop_drive`: 폰이 쇼핑 화면을 열지 못했거나 결과의 꼴이 다름), `search_failed`(`shop_api`: 쇼핑 검색 API를 부르지 못함, `result.reason`에 종류).

### `POST /v1/tasks/{id}/approval` — 승인·거부
요청:
```json
{"approval_id": "pJ2mX9aQ", "decision": "approve", "sha256": "3f1c…(approval.sha256 그대로)"}
{"approval_id": "pJ2mX9aQ", "decision": "reject", "reason": "받는 사람을 김팀장으로 바꿔 줘"}
```
| 필드 | 형식 | 설명 |
|---|---|---|
| `approval_id` | 문자열, 필수 | 조회 응답의 `approval.approval_id`. 다르면 409 (이미 다른 승인 요청으로 바뀐 경우) |
| `sha256` | 소문자 16진수 64자, 선택(**보내기를 권장**) | 사용자에게 보여 준 초안의 `approval.sha256`. 고정한 초안과 다르면 409 `hash_mismatch`(실행 안 함). 생략하면 `approval_id`가 가리키는 고정 초안(그 해시)을 승인한 것으로 본다(v1 호환) |
| `decision` | `"approve"` 또는 `"reject"`, 필수 | |
| `reason` | 문자열 500자 이하, 선택 | **거부 사유가 있으면** 에이전트가 고쳐서 다시 승인을 요청한다(최대 2번, 새 `approval_id`). **사유 없이 거부하면** 작업이 `failed`(`approval_denied`)로 끝난다 |

응답 `200`: 작업 객체. 승인 후 서버는 화면을 다시 확인해 승인한 해시와 같을 때만 저장한다.

**주의: 승인 POST 직후 응답은 아직 같은 승인 대기 상태로 올 수 있다.** 에이전트가 응답을 받아 처리하기까지 잠시 걸리기 때문이다.
이때 `status`는 `awaiting_approval`, `approval.approval_id`는 방금 보낸 것과 같고, `approval.received`에 `"approve"` 또는 `"reject"`가 들어 있다.
앱은 `received`가 있으면 "처리 중"으로 표시하고 다시 승인 버튼을 띄우지 않는다. 같은 승인 요청에 다시 POST하면 409 `conflict`.
거부 사유를 보낸 경우, 새 초안은 **다른 `approval_id`·`draft_id`·`sha256`** 으로 온다 — `approval_id`가 바뀐 것을 보고 새 카드를 띄운다.

### 본인 주소 발송 (v3, D50)
"나한테 회의 요약 보내 줘" 같은 부탁을 **사용자 본인 주소로만** 실제 메일로 보낸다. 모델은 초안을 낼 뿐이고, 정책 검사 → 초안 고정 → 승인 → 10초 대기 → 해시 재확인 → 발송 → 도착 확인은 서버 코드가 한다.
- 보낼 수 있는 주소(본인 주소) = **앱에서 연결하며 로그인이 확인된 주소**(D51) + 실행하는 쪽 설정에 적은 주소(선택). 로그인할 수 없는 주소는 등록되지 않는다. 기본 계정("나한테")은 먼저 연결한 계정이고 connect의 `default`로 바꾼다. "네이버로"·"지메일로"라고 말하면 그 계정을 쓴다. 보내는 계정 = 받는 주소.
- **계정이 없을 때**: 본인에게 보내 달라는 부탁(v3 요청)에 연결된 계정이 없으면(또는 말한 서비스의 계정이 없으면) 서버는 모델을 부르지 않고 바로 끝난 작업을 돌려준다 — `action.type: "task"`, 작업 `status: "failed"`, `result.outcome: "send_failed"`, `result.reason: "not_set"`, replies "메일 계정을 먼저 연결해 주세요." 앱은 대화창에 계정 연결 화면을 띄우고, 연결되면 같은 부탁을 새 메시지로 다시 보낸다.
- 받는 사람이 허용 목록 밖이면 보내지 않는다(승인 요청도 하지 않음). 남에게 쓰는 메일은 지금처럼 초안까지만.
- 횟수 제한: 시간당 5통, 하루 20통.

흐름과 응답:
1. **계정 확인(승인 전)**: 비밀번호가 없거나 로그인이 안 되거나 횟수 제한이면 승인을 요청하지 않고 `status: "failed"`, `result.outcome: "send_failed"`, `result.reason`.
2. **승인**: `status: "awaiting_approval"`, `approval.kind: "send"`, `action: "보내기"`, `url: "mailto:<받는 사람>"`, `fields: {"보내는 사람", "받는 사람", "제목", "본문"}`, `sha256`(계산식은 초안과 같다), `draft_id`. 승인·거부·고칠 점은 `POST …/approval` 그대로.
3. **대기**: `status: "send_pending"`, `approval` 없음,
   `send: {"draft_id", "sha256", "from", "to", "fields", "send_at", "cancel_until", "now"}` — 시각은 모두 UTC ISO 8601, `now`는 서버 현재 시각(남은 초 = `cancel_until − now`).
4. **발송·확인 뒤**: `status: "completed"`, `result.approved_drafts`에 `{"draft_id", "sha256", "kind": "send", "action": "보내기", "url", "fields"}`,
   `result.report.sent: [{"draft_id", "sha256", "to", "message_id", "sent_at", "verified": "delivered" | "accepted_only", "detail"}]`.
   `delivered` = 받은 메일을 찾아 본문이 보낸 내용과 같음을 확인. `accepted_only` = 메일 서버는 접수했지만 도착을 확인하지 못함. `sent`에는 접수된 메일만 들어간다.
5. **실패**: `result.outcome: "send_failed"` + `result.reason`. 보낸 메일은 0통이고 `report.sent`는 빈 배열, `report.blocked` 첫 줄에 사람이 읽는 이유. 승인했더라도 `approved_drafts`에 발송 기록을 남기지 않는다.

| `result.reason` | 뜻 | 언제 |
|---|---|---|
| `not_set` | 연결된 계정이 없음(또는 그 계정의 비밀번호가 없음) → 앱이 계정 연결 화면을 띄운다 | 작업을 만들 때 / 승인 전 |
| `auth_failed` | 로그인 실패(앱 비밀번호가 틀리거나 폐기됨, 메일 서비스에서 IMAP/SMTP 사용이 꺼짐) | 승인 전 |
| `unreachable` | 메일 서버에 연결하지 못함 | 승인 전 / 발송 순간 |
| `rate_limited` | 시간당·하루 한도 | 승인 전 |
| `not_allowed` | 받는 사람이 허락된 주소가 아님(내 주소, 또는 `send_others`가 켜졌을 때 연락처·사용자가 직접 말한 주소) | 승인 전 |
| `rejected` | 메일 서버가 받지 않음 | 발송 순간 |

`report.sent`는 `X-Api-Version: 3`으로 만든 작업에만 있다(없으면 발송 기능을 쓰지 않은 작업).

### 내 공간 — 말로 짜는 카드 화면 (D87, features `space`)
사용자가 말한 대로 카드를 모아 한 화면을 만든다. **화면은 데이터다**: 맥은 "어떤 카드를 어떤 크기로 어느 차례에"라는 구성만 주고, 그리는 것은 앱이 미리 만들어 둔 카드·블록으로 한다. 구성은 Claude가 내고 맥의 코드가 검사해서 받아들인다.

| 요청 | 본문 | 응답 |
|---|---|---|
| `GET /v1/space` | | `{"space": S}` |
| `POST /v1/space/edit` | `{"text": "메일을 맨 위에 크게, 날씨는 빼 줘"}`(1~500자) | `{"space": S, "changes": ["'날씨' 카드를 뺐어요", …], "say": "…"}` — 2~10초. `changes`는 **맥의 코드가 앞뒤 구성을 견줘 쓴 문장**(체크 표시로 보여 주면 된다). `say`는 모델이 덧붙인 한 문장(못 한 까닭 등, 비어 있을 수 있다). 바뀐 것이 없으면 `changes: []` |
| `POST /v1/space/undo` | | 한 번 되돌린다 → 같은 꼴. 되돌릴 것이 없으면 404 |
| `DELETE /v1/space` | | 비운다 → 같은 꼴 |
| `POST /v1/space/refresh` | `{"card": "c_…"}`(생략하면 맥이 채우는 카드 전부), `"force": false`면 15분 안에 채운 것은 건너뛴다 | `{"space": S}` — **오래 걸릴 수 있다**(카드 하나에 0~20초: 메일·말로 만든 카드는 모델을 부른다). 앱은 카드마다 따로 부르거나 기다리는 표시를 둔다 |

```json
S = {"v": 1, "can_undo": true, "updated_at": "2026-11-16T14:00:37+09:00",
     "cards": [
       {"id": "c_Ab12Cd34", "type": "meeting_todos", "size": "large", "title": "어제 회의에서 내가 할 일", "source": "app"},
       {"id": "c_Ef56Gh78", "type": "weather", "size": "small", "title": "날씨", "source": "app"},
       {"id": "c_Ij90Kl12", "type": "calendar_today", "size": "small", "title": "오늘 일정", "source": "app"},
       {"id": "c_Mn34Op56", "type": "mail_today", "size": "medium", "title": "오늘 온 메일", "source": "mac", "state": "ok", "data_at": "…",
        "data": {"count": 5, "services": ["네이버", "지메일"], "failed": [], "items": [{"time": "오전 9:10", "from": "…", "subject": "…", "service": "지메일"}],
                 "blocks": [{"kind": "text", "text": "…"}]}},
       {"id": "c_Qr78St90", "type": "custom", "size": "small", "title": "원달러 환율", "ask": "오늘 원달러 환율 알려줘", "source": "mac", "state": "ok", "data_at": "…",
        "data": {"blocks": [{"kind": "stat", "label": "원달러 환율", "value": "…", "note": "…"}, {"kind": "list", "items": [{"title": "…", "subtitle": "…", "trailing": "…"}]}]}},
       {"id": "c_Uv12Wx34", "type": "routine_result", "size": "medium", "title": "아침 메일 브리핑", "routine_id": "rt_…", "source": "mac", "state": "unavailable",
        "why": "이 예약은 아직 돈 적이 없어요."}]}
```
- **카드 종류 `type`**: `calendar_today`(폰의 캘린더) · `weather`(앱의 날씨) · `meeting_todos`(앱의 회의 기록에서 나온 할 일·결정) — 이 셋은 `source: "app"`, **앱이 자기 데이터로 채운다**(맥은 내용을 모른다). `mail_today` · `routine_result`(`routine_id`의 마지막 실행) · `custom`(말로 만든 카드, `ask`에 무엇을 보일지) — `source: "mac"`, 맥이 `data`를 준다. **모르는 `type`은 그리지 않고 건너뛴다**(종류는 늘어난다).
- **홈 화면 위젯용 `GET /v1/space/widget`(features `space_widget`, 2026-11-16 밤 — 사용자 결정: 위젯을 만들고, 잠금 화면에 보여도 된다)**: `{"widget": {"v": 1, "updated_at": "…", "items": [{"id", "type", "title", "source", "state"?, "value"?, "line"?, "date"?, "new_count"?}, …]}}`(8개까지, 구성의 차례대로). **모델을 부르지 않고 바로 답한다**(이미 채워 둔 내용에서 코드가 한 줄을 뽑는다 — 위젯의 짧은 실행 시간에 맞춤). `source: "app"`인 카드(일정·날씨·회의 할 일)는 이름만 오고 위젯이 자기 데이터로 채운다. 맥이 채우는 카드: `value`(큰 값 — 메일은 "8통", 큰 값 카드는 그 값, 날짜 카드는 YYYY-MM-DD와 함께 `date`), `line`(40자까지 한 줄 — 이름표·첫 줄 "… 외 2건"·"챙길 메일 1통"), 채울 수 없으면 `state: "unavailable"`과 `line`에 까닭. 위젯에서 카드를 새로 채우려면 `refresh`가 필요한데 오래 걸리므로, 위젯은 이 주소만 부르고 새로 채우기는 앱이 열렸을 때 한다(예약에 이은 카드는 예약이 돌 때 맥이 미리 채워 둔다).
- **추천 카드 `suggestions`(2026-11-16 밤)**: `S`에 `suggestions: [{"label": "오늘 온 메일", "say": "오늘 온 메일 카드 넣어줘"}, …]`(6개까지)가 온다 — 지금 구성에 없고 바로 만들 수 있는 카드를 맥의 코드가 고른 것(연결된 메일 계정·예약·로그인해 둔 사이트·공개 정보). 누르면 `say`를 `POST /v1/space/edit`의 `text`로 보낸다. 앱 권장: 빈 화면의 예시 문장 자리와, 편집 화면의 입력창 위에 칩으로.
- **새 블록 둘(2026-11-16 밤)**: `{"kind": "pairs", "items": [{"label": "최고", "value": "15도"}, {"label": "최저", "value": "6도"}]}`(이름·값 짝 2~4개 — 나란히 놓는다) · `{"kind": "date", "label": "성탄절", "date": "2026-12-25", "note": "금요일"}`(다가오는 날 하나 — 앱이 오늘부터 며칠 남았는지 세어 "D-39"처럼 보인다. 지난 날짜는 오지 않는다). 모르는 `kind`는 건너뛰면 된다.
- **만들 때 바로 가르기·되묻기·대화에서 고치기(2026-11-16 밤)**:
  - `POST /v1/space/edit` 응답에 **`"asking": true`**가 올 수 있다 — 무엇을 보일지 정하려면 더 알아야 할 때("주식 보여줘" → 어느 종목?). 이때 구성은 그대로이고 `say`가 되묻는 한 문장이다. 사용자의 다음 `edit`을 그 답으로 읽는다(5분 안). 앱 권장: `say`를 묻는 말로 보이고 입력창을 그대로 둔다.
  - 계정에 들어가 봐야 하는 카드(결제·주문 내역 등)는 **만들자마자** `state: "unavailable"`, `why: "매일 모아 와야 해요"`, `action`(매일 아침에 모아보기)으로 온다 — `refresh`를 기다리지 않는다.
  - 비서가 볼 길이 없는 것(폰의 알림·카카오톡·걸음 수 등)은 카드를 만들지 않고 `say`로 알린다. 시시각각 변하는 시세(주가·코인·주가지수)도 만들지 않는다("정확한 시세는 아직 볼 수 없어서 그 카드는 만들지 않았어요" — 사용자 결정 2026-11-16). 빼 달라는 말이 없는데 모델이 카드를 빠뜨리면 맥이 되살린다(말하지 않은 카드는 사라지지 않는다).
  - **대화에서도 고친다**: `POST /v1/messages`에 "내 공간에 환율 카드 넣어줘"처럼 말하면 맥이 내 공간을 고치고, 응답 맨 위에 **`space`**(바뀐 뒤의 구성 S)가 실린다. 말풍선은 `changes`의 문장들(`by: "mac"`). 되물을 때는 `action: {"type": "ask", "choices": []}`. 앱 권장: 응답에 `space`가 있으면 내 공간 화면을 그것으로 바꾼다.
- **지난번과 달라진 것(2026-11-16 저녁)**: 맥이 카드마다 지난 결과를 견준다 — **메일 카드와 예약에 이은 카드만**(같은 곳에서 같은 방식으로 오는 내용). 웹 검색으로 채운 카드(예약에 잇지 않은 `custom`)에는 `changed_at`·`delta`·`trend`·`new`가 오지 않는다(검색은 채울 때마다 다른 기사의 값을 가져와 차이가 실제 움직임이 아니다). 카드에 `changed_at`(내용이 마지막으로 바뀐 때 — 처음 채운 것은 바뀐 것이 아니라 없다), `data.new_count`와 목록 줄의 `"new": true`(지난번에 없던 줄), `data.delta: {"diff": 4.5, "text": "+4.5"}`(큰 값이 숫자일 때 지난 값과의 차이), 그리고 숫자가 3번 넘게 쌓이면 `blocks` 끝에 **`{"kind": "trend", "points": [1336.0, 1340.5, 1338.0]}`**(오래된 것부터, 14개까지 — 작은 추세선용. 모르는 `kind`는 건너뛰면 된다). `POST /v1/space/refresh`의 응답에 `changed`(이번에 내용이 바뀐 카드 id 배열)가 더 붙는다. 카드의 내용은 이제 맥의 파일에 남아 서버가 다시 떠도 `empty`가 되지 않는다.
- **카드에서 바로 하기 `actions`(2026-11-16 저녁)**: 맥이 채우는 카드에 `actions`(2개까지)가 붙을 수 있다. 두 가지 꼴 — `{"label": "자세히 묻기", "say": "…"}`: 누르면 `say`를 대화로 보낸다(`action`과 같은 길). `{"label": "지금 가져오기", "run": "rt_…"}`: 누르면 **`POST /v1/routines/{id}/run`**(본문 없음 → `202 {"started": true}`, 그 예약을 지금 한 번 뒤에서 돌린다. 끝나면 카드가 채워진다 — 앱은 조금 뒤 그 카드를 `refresh`하면 된다. 도는 동안 카드는 "가져오는 중이에요"). 메일 카드의 `picks[]`에는 줄마다 `say`가 있다(그 메일을 눌렀을 때 대화로 보낼 말). `actions`는 맥의 코드가 정한 것만 온다(모델이 단추를 만들지 않는다). 기존의 `action`(채울 수 없는 카드의 단추 하나)은 그대로다.
- **카드에 이은 예약의 알림**: 카드에 이은 예약이 돌면 맥이 그 카드를 미리 채워 두고, **내용이 바뀌었을 때(와 처음 돌았을 때)만** `routine_result` 알림을 보낸다. 바뀐 것이 없으면 그 실행의 알림은 가지 않는다(승인·질문·실패 알림은 그대로 간다).
- **손으로 고치기 `POST /v1/space/arrange`(features `space_arrange`, 2026-11-16)**: 본문 `{"cards": [{"id": "c_…", "size": "small"|"medium"|"large"}, …]}` — 목록의 차례가 화면의 차례, 지금 구성에 있는데 목록에 없는 카드는 뺀 것. 판단 모델을 부르지 않는다(바로 답한다). 응답은 `edit`와 같은 꼴(`changes`는 코드가 견줘 쓴 문장, `say`는 빈 문자열). **모르는 `id`·모르는 `size`·같은 `id` 두 번이면 400이고 아무것도 바뀌지 않는다.** `size`를 빼면 그 카드의 지금 크기. 새 카드는 만들지 못한다(말로만). `undo`로 한 번 되돌릴 수 있다.
- **카드와 예약 잇기(2026-11-16)**: 카드의 `action` 단추가 보낸 말로 예약이 만들어지면 맥이 그 카드에 잇는다(말 안에 그 카드의 `ask`가 그대로 들어 있을 때 — 대화 응답에 "내 공간의 '…' 카드에 그 결과가 보여요"가 붙는다). 이은 카드는 `custom` 그대로이고 `routine_id`가 붙으며, 그 뒤로 **그 예약이 마지막으로 돈 결과**로 채워진다: 아직 안 돌았으면 `unavailable` "내일 오전 9:00에 처음 돌아요", 도는 중이면 "가져오는 중이에요", 로그인이 풀렸으면 "로그인이 필요해요", 돌았으면 `ok` + 블록, `data_at`은 예약이 돈 때. 같은 실행은 다시 다듬지 않으므로 `refresh`가 바로 답한다(새 실행을 처음 다듬을 때만 수십 초).
- **채울 수 없는 카드는 짧게(2026-11-16 실기기 빌드 60 뒤)**: `unavailable`의 `why`는 **24자 이하 한 구절**이다("연동 필요", "로그인이 필요해요", "사이트 확인이 필요해요", "메일 계정 연결 필요", "내일 오전 8:00에 처음 돌아요"). 비서가 제 사정을 설명하는 글("…는 제가 직접 볼 수 없어요")은 카드에 오지 않는다 — 값 없이 그런 말뿐인 답은 `unavailable`("연동 필요")이 되고, 값이 있으면 그 값만 온다. 할 수 있는 일이 있으면 카드에 **`action: {"label": "매일 아침에 모아보기", "say": "매일 아침 9시에 …"}`**가 붙는다 — 단추를 누르면 앱이 `say`를 대화(`POST /v1/messages`)로 보내면 된다(예약 화면의 + 와 같은 방식: 비서가 예약을 만든다). `action`이 없으면 단추도 없다. `changes`의 조사는 맥이 앞말에 맞춰 쓴다("'날씨'를", "을(를)" 표기 없음). `say`는 한 문장·40자 안쪽을 목표로 온다.
- **메일 카드의 내용(2026-11-16 실기기 뒤 바꿈)**: `mail_today`의 `data`에 `headline`(맨 위 한 줄, 30자까지 — "챙길 메일 1통"), `picks`(챙겨야 할 메일만 3개까지: `{"title", "note", "service"}`), `rest`(나머지를 묶은 한 줄, 50자까지)가 더 온다. 대화용 긴 브리핑 글은 이제 오지 않는다. 지금 빌드를 위해 `items`에는 챙길 메일이 먼저(`subject`에 "제목 — 할 일"), 챙길 것이 없으면 숫자만 다른 제목을 묶은 목록("… 외 2통")이 오고, `blocks`는 짧은 글 하나다. 앱 권장: `picks`를 "확인 필요" 표시와 함께 줄로, `rest`를 작은 회색 글로. 카드에 들어가는 글에서는 굵게 표시·글머리표·메일 주소·링크를 맥이 뗀다(모든 블록).
- **앱이 채울 수 있는 카드 알리기(2026-11-16 추가)**: `POST /v1/space/edit`의 본문에 선택 키 `app_cards`(앱이 채울 수 있는 폰 카드 `type` 배열, 예: `["weather", "meeting_todos", "calendar_today"]`). 보내지 않으면 맥은 `weather`·`meeting_todos`만 된다고 본다 — **`calendar_today`는 앱이 `app_cards`에 넣어 보낼 때만 새로 만든다**(앱에 캘린더 권한이 없어 빈 카드가 되므로. 그 전에는 부탁받으면 `say`로 "아직 만들 수 없다"고 알린다). 이미 구성에 있는 카드는 그대로 둔다. `meeting_todos`의 기본 이름은 "회의에서 나온 할 일"(누가 맡았는지는 가리지 못한다).
- **크기 `size`**: `small`(반 칸 — small 둘이 이어지면 한 줄에 나란히) · `medium`(한 줄) · `large`(한 줄, 크게 — 내용을 더 많이 보인다). 차례는 배열 순서(위에서 아래).
- **맥이 채우는 카드의 `state`**: `ok`(`data` 있음) · `stale`(15분이 지난 `data` — 보여 주되 새로 고침을 부른다) · `empty`(아직 채운 적 없음 — `refresh`를 부른다) · `unavailable`(채울 수 없다 — `why`를 카드에 보인다: "로그인이 필요해요", "메일 계정을 연결하면 보여요" 등. 값을 지어내 채우지 않는다). 카드의 내용은 맥 메모리에만 있어 서버가 다시 뜨면 `empty`다.
- **블록 `data.blocks[]`**(4개까지, 위에서 아래): `{"kind": "text", "text"}` · `{"kind": "stat", "label", "value", "note"}`(큰 값 하나) · `{"kind": "list", "items": [{"title", "subtitle", "trailing"}]}`(6줄까지) · `{"kind": "progress", "label", "ratio": 0~1}`. **모르는 `kind`는 건너뛴다.** `mail_today`는 `blocks`(요약 글)와 함께 `count`·`services`·`items`(코드가 만든 목록 5줄까지)를 준다 — 전용 카드로 그려도 되고 블록만 그려도 된다.
- 말로 만든 카드는 채울 때 그 말을 비서에게 건네 **바로 나온 답**만 블록으로 옮긴다(공개 정보 찾기, 메일 등). 사이트에 들어가야 하는 일·로그인·승인이 필요한 일은 채우지 않고 `unavailable`로 둔다. 답에 없는 숫자가 든 블록은 맥이 버린다.
- 앱 권장 흐름: 탭을 열면 `GET /v1/space` → `cards`가 비었으면 빈 화면(예시 문장) → 입력은 `POST /v1/space/edit` → 응답의 `changes`를 보여 주고 화면을 바꾼다 → `state`가 `empty`·`stale`인 카드는 `POST /v1/space/refresh {"card": id}`. 화면 시안은 저장소 밖에 따로 두었다.

### 손 넘기기 (D106, features `handover`)
사람이 해야 하는 단계(사람 확인 퍼즐 등)를 만나면 작업이 질문 카드로 멈춘다: `status: "awaiting_input"`, `question.choices: ["다 했어요", "그만하기"]`, 그리고
`question.handover: {"id": "h_…", "reason", "host", "frame": "/v1/handover/{id}/frame", "input": "/v1/handover/{id}/input", "idle_close_s": 300}`.
- `GET …/frame` → `{"jpeg_b64", "width", "height"}` — 브라우저의 보이는 화면(CSS 픽셀 크기). 0.3~1초마다 다시 받으면 된다.
- `POST …/input` — `{"kind": "tap", "x", "y"}` · `{"kind": "drag", "x", "y", "x2", "y2"}` · `{"kind": "wheel", "dy", "dx"?, "x"?, "y"?}` · `{"kind": "text", "text"}`(1~500자) · `{"kind": "key", "key"}`(Enter·Backspace·Tab·Escape·ArrowLeft/Right/Up/Down·Delete·Space). 좌표는 frame의 width·height 기준. → `{"ok": true}`, 범위 밖이면 400.
- 끝: 사용자가 질문에 "다 했어요"로 답하면(기존 `POST /v1/tasks/{id}/answer`) 에이전트가 화면을 다시 읽고 이어 간다. 질문이 끝나면 손 넘기기도 닫혀 404. 5분 넘게 입력이 없어도 닫힌다.

### 활동 · 전역 검색 (D103, features `activity`)
앱의 탭 구성(사용자 결정 2026-11-17): **대화 · 내 공간 · 활동 · 나 + 검색 버튼**(내 공간을 끄면 탭 3개). 회의·할 일 탭은 없어지고, 회의는 활동·검색에서, 할 일은 내 공간 카드·검색에서 찾는다.

- `GET /v1/activity?limit=50` → `{"waiting": [...], "items": [...]}`
  - `waiting`: 지금 사람을 기다리는 작업(`status`: `awaiting_approval`·`awaiting_input`·`send_pending`) `{kind: "task", id, at, title, status, conversation_id?}` — 맨 위에 따로.
  - `items`: 끝난 작업 기록과 예약 실행 기록, 최근 것부터. 작업 `{kind: "task", id, at, title, status, outcome?, card_kind?, card_title?, card_subtitle?, line?, conversation_id?, skill?, private?}`,
    예약 실행 `{kind: "routine", id, at, title, status, routine_id, line, task_id?, conversation_id?}`.
  - 끝난 작업은 맥 파일에 300개까지 남는다(서버가 다시 떠도 남는다). **로그인한 사이트에서 읽은 결과(`private: true`)는 `line`이 없다** — 내용은 남기지 않는다.
  - 회의(녹음)는 폰에 있으니 앱이 자기 목록을 같은 타임라인에 섞으면 된다.
- `GET /v1/search?q=…&limit=30` → `{"q", "items": [...]}` — 맥에 있는 것만: 작업 기록(제목·카드·첫 줄), 예약 실행 기록, 예약(`kind: "routine_def"`). 낱말이 모두 들어간 것, 최근 것부터, 줄마다 `snippet`(낱말 주변 140자). 회의 원문·요약·할 일은 폰이 찾는다(D7).

### 예약해 둔 일 (D80, features `routines`)
정한 때에 정한 말을 맥이 대신 건넨다(그 말을 사용자가 그때 한 것과 같다). 보내기·누르기 같은 바꾸는 일은 그때도 승인 카드에서 멈춘다.

**예약 객체**
```json
{"id": "rt_…", "title": "아침 브리핑", "prompt": "오늘 온 메일 요약해줘", "enabled": true,
 "schedule": {"kind": "daily", "times": ["07:30"]},
 "when_text": "매일 오전 7:30", "next_run_at": "2026-11-17T07:30:00+09:00", "last_run_at": null, "last_status": null, "done": false}
```
- `schedule.kind`: `once`(+`at`: "YYYY-MM-DDTHH:MM", 맥의 시각) | `daily`(+`times`) | `weekly`(+`days`: mon…sun, `times`). 화면에는 `when_text`를 그대로 쓰면 된다.
- `next_run_at`: 꺼져 있거나 끝난 한 번짜리는 null. `last_status`: `done`(답이 나옴) | `task`(작업이 됨) | `needs_reply`(되물음) | `needs_login` | `failed` | `missed`(맥이 꺼져 있어 놓침).
- 참고 앱의 묶음: `kind == "once"` → 일회성, 그 밖 → 반복성, `enabled == false` → 일시 중지.

| 요청 | 본문 | 응답 |
|---|---|---|
| `GET /v1/routines` | | `{"routines": [...]}` |
| `POST /v1/routines` | `{"title"?, "prompt", "when"}` | 201 `{"routine", "routines"}` |
| `POST /v1/routines/{id}` | `{"enabled"?, "title"?, "prompt"?, "when"?}` | `{"routine", "routines"}` |
| `DELETE /v1/routines/{id}` | | `{"routines": [...]}` |
| `GET /v1/routines/runs` | | `{"runs": [{"id": "rr_…", "routine_id", "title", "status", "due_at", "ran_at", "conversation_id"?, "replies"?, "task_id"?}]}` 최근 것부터 20건 |
| `GET /v1/routines/runs?routine_id=rt_…&limit=20` | | 그 예약의 기록만(최신 먼저, `limit` 1~100). 작업이 된 실행은 끝난 뒤 `task_status`·`task_result`(문장 배열)가 붙는다 |
| `GET /v1/routines/runs/{rr_id}` | | `{"run": {…}}` — 실행 하나. 없으면 404 |

- `when`(문자열, 만들 때·바꿀 때): `"daily 07:30"` · `"daily 11:00,17:30"`(하루 4번까지) · `"weekly mon,wed,fri 09:00"` · `"once 2026-12-05 10:00"`. 꼴이 아니거나 지난 때면 400 `invalid_request`. 20개를 넘으면 409 `too_many`. 없는 id는 404.
- **지침 `notes`(2026-11-16 추가)**: 예약마다 붙여 두는 긴 설명(여러 줄, 4000자까지 — 무엇을 어떻게 볼지). 목록·만들기·바꾸기의 예약 객체에 `notes`(없으면 빈 문자열)가 있고, `POST /v1/routines`·`POST /v1/routines/{id}`의 본문에 `notes`(문자열)를 줄 수 있다(빈 문자열이면 지운다). 돌 때 할 말 뒤에 붙어 간다. 대화로도 적는다("아침 브리핑은 보안 메일을 맨 위에 말해줘" → 그 예약의 지침 끝에 한 줄 더해지고 응답의 `routines`에 실린다). 앱 권장: 예약 상세 화면에 할 말·지침·때·기록을 보여 주고 지침을 고칠 수 있게.
- **실행 기록은 파일에 남는다(2026-11-16, 사용자 결정)**: 서버가 다시 떠도 `GET /v1/routines/runs`가 그대로 돌려준다(200개까지, 그때의 답 `replies` 포함). 예약을 지우면 그 예약의 기록도 지워진다. 예전의 "맥에 남아 있지 않아요"는 기록이 200개를 넘겨 밀려났거나 예약을 지운 때만 난다.
- **+ 단추(참고 앱의 방식, 사용자 지시 2026-11-16)**: 예약 화면의 + 는 입력 화면을 따로 띄우지 않고 대화창으로 가서 `"새 예약 작업을 만들고 싶어."`를 사용자의 말로 보낸다 → 비서가 할 일과 때를 묻고, 답을 받으면 예약한다(실제 판단 모델로 3마디 주고받기 확인).
- 대화로도 된다: "매일 아침 8시에 메일 요약해줘", "아침 브리핑 꺼줘", "예약 뭐 있어?". 그 답에는 `routines`(바뀐 뒤의 목록)가 붙는다 — 앱이 예약 화면을 새로 그릴 수 있다. `action.type`은 `none`(또는 어느 예약인지 묻는 `ask`)이다.
- **돌았을 때**: `runs`의 `replies`(말풍선, 대화 응답과 같은 꼴)와 `conversation_id`(그 대화를 이어 갈 수 있다)·`task_id`(작업이 됐으면 — 상태는 `GET /v1/tasks/{id}`). 실행 기록은 맥의 메모리에만 있어 서버를 다시 띄우면 사라진다.
- **알림**: 답이 바로 나온 실행은 `type: "routine_result"`, `task_id`에 실행 id(`rr_…`), `conversation_id`. 작업이 된 실행은 그 작업의 알림(`approval_needed`·`task_done` …)이 간다.
- 맥이 그 시각에 켜져 있어야 한다. 1시간 넘게 늦으면 돌리지 않고 `missed`.

### 연락처 (`contacts`, D78)
- `POST /v1/messages`의 `contacts`: `{"표시 이름": "메일 주소"}` 객체, **500명까지**. 이름 1~50자, 주소는 메일 형식(틀린 항목이 하나라도 있으면 400 `invalid_request` — 앱이 걸러서 보낸다).
- (2026-11-16, D85) **공통 로그인의 QR 방법**: 방법을 묻는 `need_choice`의 `choices`에 "QR 코드로 로그인"이 있을 수 있다(글 그대로 돌려보내면 된다 — 새 키 없음). 고르면 `need_approval`에 `qr: {"mime": "image/png", "b64"}`와 (있으면) `number`가 온다 — 네이버 QR과 같은 꼴이지만 **호스트가 네이버가 아니다**(`host`에 그 사이트의 로그인 호스트). 그 사이트의 앱으로 찍어야 하므로, 앱 권장: 그림을 크게 보여 주고 저장·길게 눌러 열기를 막지 않는다. QR을 해독해 주소를 열 때 nid.naver.com만 받던 검사는 `host`와 같은 등록 도메인이면 받도록(또는 해독하지 않고 그림만). 그림이 바뀌면(새로 받기) `step`이 오른다. 만료되면 `need_choice` ["새 QR 받기", "그만하기"].
- (2026-11-16) **승인·질문 카드의 화면 캡처**: 로그인한 사이트 안에서 하기(`site_do`)의 `approval`·`question` 객체에 `attachment`가 붙을 수 있다(없을 수도 있다) — `{"id": "p1", "type": "screenshot", "mime", "width", "height", "bytes", "page_host", "caption", "sensitive": true, "expires_at", "path": "/v1/tasks/{id}/attachments/p1"}`, 보고의 `attachments[]`와 같은 꼴·같은 받는 법. 승인하려는 단추가 있는 그 화면이다. 앱 권장: 카드에 그림을 함께 그린다(실기기: 어느 리뷰인지 못 보고 승인을 물었다). `site_do`의 `report.sources`는 이제 마지막에 있던 곳 하나만 온다("열기" 줄이 여러 개 붙지 않게).
- (2026-11-16) 메일 브리핑은 서비스를 말하지 않으면 연결된 계정을 모두 읽는다. `POST /v1/mail/briefing`의 응답에 `services`(읽은 곳의 이름 배열)·`failed`(읽지 못한 곳)가 더 붙고, 계정이 여럿이면 `items[]`에 `service`가 붙는다(앱은 모르는 키를 무시하면 된다).
- **`contacts_no_email`(선택, 2026-11-16 추가)**: 연락처에 있지만 **메일 주소가 없는 사람의 이름** 배열(`["이름", …]`, 500개까지, 이름 1~50자 — 꼴이 다른 원소는 버리고 오류를 내지 않는다). 보내면 그 대화의 값을 바꾼다(생략하면 이전 값 유지). 맥은 사용자의 말에 이름이 나온 사람만 대화 판단(Claude)에 알려, "연락처에 없다"가 아니라 "연락처에 있는데 메일 주소가 없다"고 되묻게 한다(`action.type: "ask"`). 이 사람들은 받는 사람이 되지 못한다 — 사용자가 주소를 직접 치면 그 주소로 간다. `contacts`에 있는 이름이 여기에도 있으면 `contacts` 쪽을 쓴다.
- `contacts`에는 메일 주소가 있는 사람만 넣는다. 주소가 여럿인 사람은 하나만(또는 "이름 (회사)"처럼 이름을 나눠 여러 항목). 같은 이름이 여럿이면 구분되는 이름으로.
- 보내는 때: 대화마다 **첫 말에 한 번**이면 된다(맥이 그 대화 동안 기억한다). 다시 보내면 통째로 바뀐다. 안 보내면 그대로.
- 맥은 메모리에만 둔다(파일·기록에 쓰지 않는다). 20명을 넘으면 사용자의 말에 이름이 나온 사람의 **이름만** 대화 판단(Claude)으로 간다. 주소는 가지 않는다.

### 다른 사람에게 보내기 (D77, features `send_others`)
- 앱이 할 일은 거의 없다: `POST /v1/messages`에 지금처럼 `contacts`({이름: 주소})를 보내면 된다. 받는 사람은 **연락처의 이름**이거나 **사용자가 대화에 직접 친 주소**여야 한다. 그 밖(회의 자료·화면에 적힌 주소, 모르는 이름)은 `ask`로 되묻는다.
- 승인 카드: `approval.kind: "send"` 그대로 + **`approval.external: true`**(받는 사람이 내가 아니다). `fields["보내는 사람"]`은 내 계정, `fields["받는 사람"]`은 남의 주소. 권장: `external`이면 받는 사람을 눈에 띄게 그린다(되돌릴 수 없는 발송이다).
- 보낼 계정: 말에 서비스가 있으면("지메일로 보내줘") 그 계정, 없으면 기본 계정. 연결된 계정이 없으면 본인 발송과 같이 `send_failed`/`not_set`.
- 결과: `report.sent[0]`에 `external: true`, `verified: "accepted_only"`, `detail`에 "받는 분의 도착은 확인할 수 없음". **"도착 확인됨"이라고 그리지 않는다.**
- 한 번에 한 사람. 참조·첨부·답장은 없다.

### `GET /v1/mail/accounts` — 메일 계정 상태 (`mail_connect`)
```json
{"accounts": [{"provider": "naver", "address": "<주소>", "status": "ready", "default": true, "checked_at": "2026-11-12T00:10:00+00:00"},
              {"provider": "gmail", "address": "<주소>", "status": "not_set", "default": false, "checked_at": null}]}
```
본인 주소를 전부 돌려준다(기본 계정이 맨 앞, 연결한 계정이 없으면 빈 배열 — 앱은 주소를 직접 입력하는 연결 화면을 보여 준다). `status`: `ready` | `auth_failed` | `unreachable` | `not_set`. 조회할 때마다 로그인하지 않고 마지막 확인 결과다. 비밀번호는 어떤 응답에도 없다.

### `GET /v1/mail/today` · `POST /v1/mail/briefing` — 오늘 받은 메일 (`mail_read`, D54)
연결된 메일 계정(기본 계정)의 받은편지함을 IMAP으로 **읽기 전용**으로 연다. 읽음 표시를 바꾸지 않는다.
- `GET /v1/mail/today` → `{"count": 10, "items": [{"time": "오후 2:29", "from": "보낸 사람", "subject": "제목", "seen": false}, …]}` — 최신 먼저, 최대 50통. 코드가 가져온 목록 그대로(모델을 쓰지 않는다). 1~2초.
- `POST /v1/mail/briefing` → 위와 같은 `count`·`items`에 `"summary"`(로컬 모델이 쓴 브리핑 글)를 더한다. 20~40초. 0통이면 모델을 부르지 않고 "오늘 온 메일이 없어요."
- 오류: 409 `not_set`(연결된 계정 없음) / `auth_failed` / `unreachable`, 502 `model_error`.
- 대화: 메일 계정이 연결돼 있으면 `POST /v1/messages`의 "오늘 온 메일 요약해줘" 같은 말에 서버가 브리핑을 만들어 `action.type: "none"`의 답으로 돌려준다(작업을 만들지 않는다. 메일 내용은 서버 대화 기록에 넣지 않는다).
- 받은편지함만 본다(네이버의 프로모션 같은 스마트메일함은 포함하지 않는다). 메일 내용은 맥의 로컬 모델에만 들어간다.

### `POST /v1/mail/accounts/check` — 다시 확인
본문 없음. 보관한 비밀번호로 계정마다 로그인해 보고 같은 형식으로 답한다(보내지 않음). 최대 20초쯤 걸릴 수 있다.

### `POST /v1/mail/accounts/connect` — 계정 연결
```json
{"provider": "naver", "address": "<내 메일 주소>", "app_password": "<앱 비밀번호>", "default": false}
```
**로그인을 확인한 뒤에만** 비밀번호를 실행하는 쪽의 보안 저장소에 보관하고, 그 주소를 본인 주소로 등록한다(서버를 다시 띄워도 남는다). 응답 `200`은 계정 목록과 같은 형식이고, 그 주소의 `status`가 `ready`면 연결된 것이다. `auth_failed`·`unreachable`이면 보관도 등록도 하지 않았다 — 이때 새 주소는 응답 목록 끝에 그 상태로 한 번 실려 온다(이미 연결된 주소였다면 보관한 값은 그대로 둔다).
- 일반 로그인 비밀번호가 아니라 메일 서비스에서 만든 **앱 비밀번호**여야 한다(네이버는 메일 환경설정의 IMAP/SMTP "사용함"도 필요). 안내: `docs/skills/mail_naver.md`, `mail_gmail.md`.
- `default: true`(선택)면 이 계정을 기본 계정으로 한다. 없으면 먼저 연결한 계정이 기본이다.
- **쓰기 전용**: 비밀번호를 돌려주는 응답은 없다. 서버는 본문을 기록에 남기지 않고 모델에 넣지 않으며 오류 응답에 되돌려 쓰지 않는다.
- `403 not_allowed`: 지원하지 않는 메일 서비스의 주소(네이버·지메일 외). `403 too_many_accounts`: 연결 계정 5개 한도. `429 rate_limited`: 주소당 10분에 5번, 전체 10분에 15번. `400 provider_mismatch`: `provider`가 주소의 도메인과 다름. `400 invalid_request`: 형식 오류.
- 앱 비밀번호의 공백은 서버가 뺀다. `provider`는 선택이다(주소의 도메인으로 정한다).

### `POST /v1/mail/accounts/disconnect` — 연결 해제
`{"address": "<주소>"}` → 보관한 비밀번호를 지우고 계정 목록을 돌려준다. 앱에서 연결한 주소는 목록에서도 빠진다(설정에 적은 주소는 `not_set`으로 남는다). 연결되지 않은 주소면 403 `not_allowed`.

### 사이트 로그인 (`site_login`, D52)
에이전트 브라우저가 실제 사이트의 로그인 화면을 열고, **사용자가 앱에서 직접** 로그인한다. 로그인 단계는 로컬 모델이 아니라 서버 코드가 진행한다.
- 아이디·비밀번호·인증 문자는 브라우저 칸에 넣고 버린다. 서버는 저장하지 않고 기록·응답·오류·모델 입력에 넣지 않는다. 앱도 저장하지 않는다.
- 남는 것은 **로그인된 상태**(쿠키)뿐이고 실행하는 쪽의 **암호화 저장소**에만 둔다(다음에 다시 로그인하지 않게). 로그인용 브라우저는 일회용이라 닫으면 아무것도 남지 않는다. `logout`이 보관한 상태를 지운다.
- 캡차·2단계 인증은 우회하지 않는다. 화면이 요구하는 것을 사용자에게 그대로 묻는다. 모르는 화면이면 멈춘다.
- 지금은 로그인까지만 된다. 로그인된 브라우저를 쓰는 작업은 아직 없다.

| 주소 | 뜻 |
|---|---|
| `GET /v1/sites` | `{"sites": [{"site": "naver", "name": "네이버", "methods": ["qr", "password"], "host": "mail.naver.com", "status": "logged_in" \| "logged_out", "checked_at"}]}` — 마지막으로 아는 상태. 주소로 배운 사이트의 `name`은 사용자가 부른 이름(`aliases`의 첫째)이 있으면 그것, 없으면 주소(D120, 앱 D142) |
| `GET /v1/sites/{site}/icon` | 맥의 에이전트 브라우저가 그 사이트에서 받아 둔 아이콘 `image/png`(64px 안팎, D110). 아직 없으면 404 `icon_not_found` — 앱은 폰에서 직접 받는다 |
| `POST /v1/sites/{site}/login` `{"method": "qr" \| "password"}` | 로그인 시작 → `{"login": L}`. 진행 중이던 로그인은 `cancelled`가 된다. 10분에 5번(429 `rate_limited`) |
| `GET /v1/sites/{site}/login/{login_id}` | `{"login": L}` — 1~2초마다 조회. 조회를 멈춰도 로그인은 계속된다. 끝난 뒤 10분까지 조회되고 그 뒤 404 `unknown_login` |
| `POST …/login/{login_id}/input` `{"step": n, "values": {…}}` | 사용자의 입력·선택 제출 → `{"login": L}`(state `working`). 지난 step이면 409 `stale_step` — 같은 step은 한 번만 제출된다. 응답이 없으면 다시 보내지 말고 조회부터 한다 |
| `POST …/login/{login_id}/cancel` | 취소 → `{"login": L}` |
| `POST /v1/sites/{site}/check` | 저장된 로그인이 살아 있는지 실제로 열어 확인(10~20초) → `{"sites": […]}`. 로그인 진행 중이면 409 `busy` |
| `POST /v1/sites/{site}/logout` | 저장된 로그인 상태를 지움 → `{"sites": […]}` |

`L = {"login_id", "site", "method", "state", "step", "message", "expires_in_s", …}`. `step`은 사용자에게 보일 화면이 바뀔 때마다 1씩 는다. `expires_in_s`는 끝나지 않은 모든 상태에 온다(로그인 전체 제한 10분, QR은 QR 남은 시간).

| state | 더 오는 것 | 앱이 할 일 |
|---|---|---|
| `starting`, `working` | — | 진행 표시, 계속 조회 |
| `need_input` | `fields: [{"name", "label", "secret"}]`, (캡차면) `image: {"mime", "b64"}`, (틀렸으면) `error: "wrong_input"` | 입력칸을 그리고 `input`으로 보낸다. `name`은 `id`·`password`·`captcha`·`code`뿐 — 모르는 이름이면 입력받지 않는다 |
| `need_approval` | QR이면 `qr: {"mime": "image/png", "b64"}`·`number` | 사용자가 폰에서 승인. QR은 그림을 해독한 주소(https, 호스트 nid.naver.com)를 열고 `number`를 고르게 한다. 2단계 인증은 `message`만 보여 준다. 서버가 화면을 지켜보다 상태를 바꾼다 |
| `need_choice` | `text`, `choices` | 고른 것을 `{"values": {"choice": "…"}}`로 보낸다 |
| `logged_in` | — | 끝 |
| `failed` | `reason`: `wrong_input`(3번 틀림) \| `blocked`(사이트가 막음) \| `unsupported_page`(모르는 화면) \| `browser_error` \| 공통 로그인에서: `not_https`(암호화되지 않은 화면이라 입력받지 않음) \| `host_refused`(넘어간 호스트를 사용자가 거절) \| `unsupported_login`(로그인 칸을 못 찾음·가입 화면·칸이 4개 초과·**로그인은 됐지만 그 상태가 다음에 열 때 이어지지 않음** — 안내 글은 `message`) | 사유 표시 |
| `expired`, `cancelled` | — | 다시 시작할 수 있다 |

**공통 로그인(D70) — 네이버가 아닌 사이트.** 형식은 위와 같다(새 키 없음). `GET /v1/sites`에 사용자가 맥에 적어 둔 사이트가 더 보이고(`methods: ["password"]`), 그 `site` 키로 같은 주소를 쓴다.
- `need_input`: 칸 이름은 그대로 `id`·`password`·`code`·`captcha`(이 가운데 화면에 있는 것만 — 두 쪽짜리 로그인은 `id`만 먼저, 그다음 `password`만). `label`은 그 화면의 칸 이름이다. `message`에 **지금 입력받는 화면의 호스트**가 들어 있다("accounts.example.com 화면이에요. …").
- `need_choice`가 두 군데에서 온다. ① 로그인이 다른 호스트로 넘어갔을 때, 값을 받기 전에: `choices: ["계속", "그만하기"]`. ② 제출한 뒤 로그인 칸이 사라졌을 때: `text`에 그 화면의 글 앞부분(300자 이하)과 "로그인이 끝났나요?", `choices: ["로그인됐어요", "아직이에요", "그만하기"]`.
  "아직이에요"는 폰 승인·인증을 마친 뒤 다시 보게 한다(같은 물음이 새 `step`으로 다시 온다). "로그인됐어요"인데 확인 화면에 로그인 칸이 다시 나오면 `need_input`으로 돌아간다.
  (D120) 사이트가 로그인 실패를 알린 화면이면: 로그아웃이 보이고 로그인 칸이 없으면 묻지 않고 서버가 확인해 넘어간다. 아니면 `text`에 그 안내와 "다시 해 볼까요?", `choices: ["다시 해 보기", "로그인됐어요", "그만하기"]` — "다시 해 보기"는 로그인 화면을 다시 열어 `need_input`부터(새 키 없음, 앱은 고르는 말을 그대로 단추로 그리면 된다).
  `text`의 화면 글은 사용자의 것일 수 있다(이름 등) — 앱은 보여 주기만 하고 기록하지 않는다. 서버도 기록하지 않고 Claude로 보내지 않는다.
- **주소로 시작한 사이트**: 사용자가 대화에서 "example.com 로그인하자"라고 하면 응답이 `action: {"type": "site_login", "site": "example-com"}`로 온다 — `site`는 주소에서 만든 키(영문 소문자·숫자·`-`, 32자 이하)이고, 그 키로 같은 로그인 주소를 쓴다. `GET /v1/sites`에는 그때부터 그 사이트가 `name: "example.com"`으로 보인다(로그인에 성공하면 서버가 다시 떠도 남는다).
- `need_choice`가 한 군데 더 온다 — **로그인 방법**: `text` "○○에 어떤 방법으로 로그인할까요?", `choices` 예 `["아이디·비밀번호로 로그인", "카카오로 로그인", "구글로 로그인", "그만하기"]`(화면에 있는 방법만). 소셜 로그인을 고르면 그 회사의 로그인 화면(예: accounts.kakao.com)의 칸이 `need_input`으로 온다 — 그 호스트는 다시 묻지 않는다.
- `need_choice`가 소셜 로그인의 **칸 없는 중간 화면**에서도 온다(예: "이 계정으로 계속하기", 동의 화면): `text`에 그 화면의 글, `choices`는 그 화면의 단추 글 + `"기다리기"` + `"그만하기"` — 예 `["계속하기", "다른 카카오계정으로 로그인", "기다리기", "그만하기"]`. 서버는 사용자가 고른 단추만 누른다. 물음이 떠 있는 동안 화면이 스스로 넘어가면 서버가 다음 상태로 바꾼다(앱은 계속 조회하면 된다).
- 제출 뒤 사이트가 화면 위에 **확인 문제(보안문자)** 를 띄우면 `need_input`: `fields: [{"name": "captcha", …}]` 하나 + `image`(그 문제 상자의 그림, png 또는 jpeg) + `message`에 문제의 글("…가 확인 문제를 냈어요. 그림을 보고 답해 주세요 — …"). 네이버 로그인의 그림 문제와 같은 꼴이다. 사용자의 답을 서버가 그 칸에 넣는다. 틀리면 새 그림으로 다시 온다(한 로그인에 4번까지). 답 칸이 없는 종류면 `need_choice`(`["기다리기", "그만하기"]`)로 답할 수 없다고 알린다.
- 사이트가 거절했을 때의 `need_input`(`error: "wrong_input"`): `message`에 **그 사이트가 띄운 안내 글 그대로**가 들어 있다("…이 이렇게 안내했어요: “…” — 다시 입력해 주세요"). 사용자의 것일 수 있으니 앱은 보여 주기만 한다.
- 로그인 회사가 이 브라우저의 로그인을 받지 않으면(예: "브라우저 또는 앱이 안전하지 않을 수 있습니다") `failed` + `reason: "blocked"`, `message`에 어느 호스트가 받지 않았는지. 구글 로그인은 그래서 고를 방법에 넣지 않는다(로그인 방법 물음의 `text`에 뺐다고 적는다).
- 제출한 뒤 화면이 넘어가지도 거절하지도 않으면(최대 10초 지켜본 뒤) `need_choice`: `text`에 그 화면의 글과 "제출했는데 화면이 넘어가지 않았어요."(또는 "확인 절차나 안내 창이 떠 있는 것 같아요."), `choices: ["다시 입력", "기다리기", "그만하기"]`. `error: "wrong_input"`은 사이트가 거절했을 때만 붙는다(오류 안내가 떴거나 비밀번호 칸이 비워짐).
- `need_approval`이 공통 로그인에서도 온다: 소셜 로그인의 폰 승인·2단계 인증을 기다리는 동안. `message`에 그 화면의 글 앞부분과 "폰에서 승인하면 이어서 진행돼요"(QR·`number` 없음). 서버가 화면을 지켜보다 사이트로 돌아오면 `logged_in`으로 바꾼다. 3분이 지나면 `need_choice`("로그인이 끝났나요?")로 바뀐다.
- `need_input`·`need_choice`·`need_approval`에 선택 키 `host`: 지금 입력·선택을 받는 화면의 호스트(공통 로그인에서만). 앱이 값을 채울 곳을 가를 때는 `message`의 글이 아니라 이 값을 쓴다. 로그인 흐름의 `need_choice`에서 앱은 `text`만 보여 주므로 호스트는 `text`에도 들어 있다.
- 공통 로그인이 끝나면 `logged_in` 전에 `working`이 몇 초 온다(`message` "로그인됐어요. 다음에도 이어서 쓸 수 있는지 확인하고 있어요"): 서버가 보관한 로그인 상태로 새 브라우저를 열어 본다. 이어지지 않으면 `failed` + `unsupported_login`(안내는 `message`). 새 상태·새 키 없음.
- **약속(2026-11-15부터 앱이 기대고 있다)**: 로그인 `failed`의 `message`는 맥이 쓴 정해진 문장이다 — 들어가는 변수는 호스트·사이트 이름·로그인 회사 이름·오류 코드뿐이고 **사이트 화면의 글은 넣지 않는다.** 앱은 `blocked`·`unsupported_login`·`host_refused`·`not_https`에서 이 글을 그대로 보여 준다. 화면의 글을 넣게 되면 앱에 먼저 알린다(화면 글이 오는 곳은 `need_choice`의 `text`와 `need_approval`의 `message`뿐).
- 비밀번호를 폰의 키체인에 두는 것(사용자 결정)은 앱의 일이다: 저장해 둔 값을 Face ID 뒤에 같은 `input`으로 보내면 된다. 서버는 받은 값을 칸에 넣고 버린다(지금과 같다).

대화에서: `X-Api-Version: 4`를 보낸 앱이 "네이버 로그인해 줘"처럼 말하면 서버는 모델을 부르지 않고
`{"replies": [{"text": "네이버 로그인 화면을 열게요."}], "action": {"type": "site_login", "site": "naver"}}`를 돌려준다. 앱은 그 자리에 로그인 카드를 띄운다.

### 사이트 읽기 (`site_read`) — 로그인해 둔 사이트의 페이지를 읽고 답하기
사용자가 직접 로그인해 둔 사이트의 **정해진 페이지 하나**를 에이전트 브라우저로 열어, 그 화면 글을 로컬 모델이 읽고 물음에 답한다. 읽기만 한다(누르기·입력 없음).
- 시작: 새 주소는 없다. 카드의 `prompt`나 사용자의 말("네이버페이 결제내역에서 최근에 결제한 3건을 알려줘")을 `POST /v1/messages`에 보내면
  서버가 모델로 판단하지 않고 바로 작업을 만든다 → `action: {"type": "task", "task": {…}}`. 그 뒤는 보통 작업과 같다(조회 → `completed`).
  1~2분 걸린다. 승인·질문 단계는 없다.
- 결과: `result.report.done`에 답(한 덩어리 글, 줄바꿈 포함), `sources`에 연 페이지 주소(물음표 뒤는 뺀다), (켜져 있으면) `attachments`.
  **답은 사용자의 실제 개인 데이터다** — 서버는 대화 기록·기억 저장소·서버 기록에 남기지 않는다. 앱도 Claude로 가는 요청의 맥락에 넣지 않는다.
  이런 보고에는 `result.report.private: true`가 붙는다(로그인한 페이지에서 읽은 답일 때만, 없으면 false). 앱이 저장하지 않기·복사 막기 같은 것을 종류별로 할 때 쓴다(어떻게 할지는 사용자가 정한다).
- 로그인이 안 돼 있으면: 작업을 만들지 않고 `replies`에 "네이버 로그인이 필요해요…" + (`X-Api-Version: 4`면) `action: {"type": "site_login", "site": "naver", "resume": true}`.
  - `resume: true`(features `site_login_resume`) = 로그인하면 이어서 할 부탁이 있다. 앱은 `logged_in` 직후 그 사용자 말을 **새 `client_message_id`로** 한 번 다시 보낸다. "네이버 로그인해줘"처럼 로그인만 해 달라는 말의 `site_login`에는 이 키가 없다.
  - 서버도 멈춘 부탁을 기억한다: 안내 바로 뒤에 사용자가 "했어/로그인했어"라고 하면 그 부탁으로 작업을 만든다(`action.type: "task"`). 아직 로그인이 안 됐으면 같은 안내를 다시 준다.
  - 같은 대화에서 같은 화면 읽기가 이미 돌고 있으면 또 만들지 않고 `action: {"type": "none"}` + "지금 확인하고 있어요…"로 답한다. 앱의 재전송과 "했어"가 겹쳐도 작업은 하나다.
- 읽은 결과는 대화가 살아 있는 동안(24시간) 맥 메모리에 있다(서버 재시작·그 사이트 로그아웃 때 삭제). 그래서 이어지는 말이 된다:
  "아까 그거 두 번째 거 얼마였지?" → `action none`으로 답(맥의 로컬 모델이 읽은 내용으로), "이거 정리해서 내 메일로 보내줘" → 그 내용으로 메일 작업(승인 흐름은 같다). 응답의 꼴은 바뀌지 않는다.
- 열어 보니 로그인이 풀려 있으면: 작업이 `status: "failed"`, `result.outcome: "needs_login"`, `result.reason: "login_expired"`(보관한 로그인이 아예 없으면 `login_missing`),
  `result.action: {"type": "site_login", "site": "naver"}`로 끝난다. `report.blocked` 첫 줄에 "네이버 로그인이 풀렸어요. 다시 로그인해 주세요"가 들어간다(`action`을 모르는 앱에서도 이유가 보인다).
  그때부터 `GET /v1/skills`의 그 사이트 카드는 `available: false`, `needs[].reason: "expired"`가 되고, `GET /v1/sites`의 그 사이트에 `reason: "expired"`가 붙는다.
- 지금 있는 페이지: 네이버페이 결제내역 첫 화면(기능 파일 `skills/naver_pay_recent.md`). 취소·환불·결제 같은 바꾸는 말은 이 길로 오지 않는다.

### 대화 판단이 어디서 되나 (`dialog_cloud`, D59)
`features`에 `dialog_cloud`가 있으면 대화 판단(답할지·물을지·작업을 만들지·어떤 기능을 쓸지)을 **Claude**가 한다(맥이 Claude API를 직접 부른다). 없으면 맥의 로컬 모델이 한다.
실행(브라우저·메일·로그인)과 개인 내용을 읽고 쓰는 일은 어느 쪽이든 맥이 한다. 응답의 꼴은 같다. Claude를 부르지 못한 말은 그 한 번만 로컬 판단으로 처리한다.

| Claude로 나가는 것 | 나가지 않는 것(코드로 막는다) |
|---|---|
| 사용자가 친 말(메일 주소는 `[주소1]`로 가림) | 사이트에서 읽은 결과·화면 글·캡처 |
| Claude가 쓴 답 | 받은 메일의 제목·보낸 사람·본문 — **`MAIL_CLOUD=1`이면 나간다**(D82, 사용자 결정 2026-11-16): 오늘 받은 메일의 보낸 사람 이름·제목·시각과 본문 앞부분(메일당 500자)이 브리핑을 쓸 때와 그 브리핑을 이어 묻는 말에 Claude로 간다. 그때 그 말풍선의 `by`는 `claude`. 보낸 사람의 메일 주소·첨부·읽지 않은 다른 메일은 가지 않는다 |
| 일의 기록: "[작업을 만들었다: 제목]", "[… 작업이 끝났다 — 결과 r1: …확인 결과]", "[로그인이 필요해 멈췄다: …]" | 메일 주소 전부(연락처·본인·사용자가 친 것) — 주소는 맥이 채운다 |
| 연락처의 이름, "나"라는 이름표 | 아이디·비밀번호·쿠키·토큰·앱 비밀번호 |
| 앱이 붙여 보낸 회의 자료(`context`) — 사용자 결정으로 허용. 서버 설정 `DIALOG_SEND_CONTEXT=0`이면 "붙어 있다"는 사실만 | 읽은 결과로 쓴 답과 글(로컬 모델이 쓴다) |
| 지금 날짜·시각, 사이트 로그인 여부, 메일 계정 연결 여부, 할 수 있는 일 설명 | 작업의 단계·진행 글, 승인 내용 |
| (`web_lookup`일 때) 검색할 물음 한 문장 — Claude가 대화에서 풀어 쓴 것. Anthropic의 웹 검색으로 나간다 | 검색 호출에는 대화 기록·회의 자료·연락처가 가지 않는다. 메일 주소는 물음에서 뺀다 |

- 막을 수 없는 것: 사용자가 말 속에 직접 친 내용(이름·금액 등)은 그대로 간다.
- 지시로만 막는 것(코드로는 막지 못한다): 검색할 물음에 연락처에 있는 사람의 이름이나 회의 자료의 문장을 넣지 않는 것.
- 요청의 선택 키: `client_time`(features `client_time`) — `"2026-11-13T14:20:00+09:00"` 꼴. 없거나 읽지 못하면 맥 시계. `reply_to`(features `reply_to`) — `{"task_id": "<작업 id>"}`,
  사용자가 답장으로 가리킨 작업. 그 대화에서 만든 작업일 때만 쓰고, 모르는 id·꼴이 다른 값은 무시한다(오류 없음).
- 답을 쓴 곳(features `reply_by`): `replies`의 원소마다 `by`가 붙는다 — `{"text": "…", "by": "claude"}`.
  `claude`: Claude가 쓴 글(일상 답, 되묻는 말, 일을 맡았다는 한 마디, 웹 검색 답). `local`: 맥의 로컬 모델이 쓴 글(읽은 결과에 대한 답, 메일 브리핑, Claude를 부르지 못해 로컬로 처리한 말의 답).
  `mac`: 모델의 글이 아니라 코드가 정해 둔 문장(로그인 안내, 안전장치가 되묻는 말, 웹 검색 답의 출처 줄, 작업을 만들었다는 기본 문장 — 로컬 판단일 때는 말투만 로컬 모델이 바꿨을 수 있다).
  한 응답에 값이 섞일 수 있다. 모르는 값은 표시 없이 그린다. 작업의 보고·승인 본문에는 붙지 않는다(전부 맥이 만든다). `dialog_cloud`가 꺼져 있어도 붙는다(`local`·`mac`만).
- 말풍선: `replies`의 원소 하나가 말풍선 하나. `none`·`ask`는 1~4개, `task`·`site_login`은 1개, `route_claude`는 0개. 마크다운은 내지 않는다(목록은 "1. …" 줄).
- 말투: `tone` 값은 그대로 `friendly`|`formal`. Claude가 판단할 때 `friendly`(와 값이 없을 때)는 사용자가 정한 비서 말투(짧은 말풍선, 'ㅋㅋ'·이모지 가능), `formal`은 합니다체.
  말투는 채팅 답에만 쓴다 — 작업 제목·지시·메일 초안·읽은 결과의 답에는 쓰지 않는다.

### 공개 정보 웹 검색 (`web_lookup`, D60)
`features`에 `web_lookup`이 있으면 누구나 볼 수 있는 정보(날씨, 물건 가격, 가게·시설 운영 시간, 뉴스, 환율)를 묻는 말에 맥이 Claude의 웹 검색으로 찾아 답한다.
- 응답의 꼴은 평소 답과 같다: `action: {"type":"none"}`, `replies` 1~4개. 작업은 만들지 않는다. 새 키는 없다.
- 출처가 있으면 **마지막 말풍선**이 `출처: 사이트이름, 사이트이름` 한 줄이다(최대 3곳, 사이트 이름만 — 주소(URL)·마크다운 없음, 코드가 붙인다, `by: "mac"`).
  답에 인용이 붙지 않았을 때는 검색이 돌려준 페이지를 `찾아본 곳: …`으로 적는다(그 페이지를 실제로 썼다는 보장이 없어 출처라고 하지 않는다).
- 걸리는 시간: 판단 약 2초 + 검색 약 3~6초(측정). 검색하지 못하면 값을 지어내지 않고 "지금은 웹 검색이 안 돼요…" 말풍선 하나.
- 지역처럼 꼭 필요한 것이 없으면 `ask`로 되묻는다. 로그인해야 보이는 것(내 주문·메일)은 검색하지 않는다.
- 찾은 답은 대화 기록에 남아 다음 말에서 이어 쓴다("부산은?", "256기가는 얼마야?").
- Claude를 부르지 못해 로컬 판단으로 처리한 말에서는 검색하지 않는다(실시간 정보는 "확인할 수 없다"고 답한다).

### 쇼핑 — 옆에서 봐 주기 (`shop_view`, D66)
살 것을 찾는 일은 **폰의 앱 안 브라우저**에서 사용자가 직접 한다. 맥은 쇼핑 사이트에 접속하지 않는다 — 무엇을 찾을지 정해 주고, 앱이 화면에서 읽어 보낸 값을 사용자의 조건과 견줘 말해 준다.
- 요청의 `client_features`에 `"shop"`이 있을 때(앱이 앱 안 브라우저를 열 수 있을 때), 사려고 찾는 말("생수 사야 해")에:
  `action: {"type": "shop", "query": "생수 2L 6개"}` + 말풍선 1개. 주소는 앱이 만든다(맥은 검색어만 준다). 작업은 만들지 않는다. 무엇을 살지 모호하면 `ask`.
- 요청의 선택 키 `view` — 사용자가 보고 있는 화면에서 **앱이 뽑은 칸**(화면 글 통째가 아니다). 사용자가 "확인해 줘" 단추를 누르거나 그 말을 보낼 때만 붙인다.
```json
"view": {"kind": "product", "url": "https://smartstore.naver.com/…/products/…",
         "fields": {"title": "…", "price": 12900, "original_price": 15000, "member_price": true, "shipping": "3,000원",
                    "seller": "…", "arrival": "…", "stock": "…", "options": [{"name": "수량", "values": ["6개", "12개"]}]}}
```
  - `kind`: `product` | `list`(그 밖의 값 — 주문서·결제·장바구니 — 은 맥이 버린다). `list`의 `fields`는 `{"items": [{"title", "price", "mall"}]}` 8개까지.
  - 맥은 아는 칸만, 꼴이 맞을 때만 쓴다: `price`·`original_price`는 0보다 큰 정수, 글 칸은 길이 제한, `url`은 호스트만 쓴다. 읽지 못한 칸은 넣지 않는다 — 맥의 답이 "화면에서 읽지 못했어요"가 된다.
  - 이 칸들은 공개 상품 정보로 보고 대화 기록에 남기며 **Claude가 본다**. 이름·배송지·회원 정보처럼 사용자에 따라 달라지는 것은 넣지 않는다. 주문서·결제 화면에서는 `view`를 만들지 않는다.
- 확인하지 못하는 경우(값을 지어내지 않는다 — 모두 `none` + 정해 둔 말풍선 1개, `by: "mac"`): `view`에 쓸 값이 하나도 없다("화면에서 읽은 값이 없어서 확인하지 못했어요 …"),
  Claude를 부르지 못했다("지금은 화면을 확인하지 못했어요. 잠시 뒤에 다시 눌러 주세요"), Claude 판단이 꺼진 서버다("지금은 화면을 확인할 수 없어요").
- 맥의 답은 평소의 `none` + 말풍선이다. 값은 `view`에 온 그대로만 쓴다. 맥은 주문·결제·장바구니 담기를 하지 않고 그렇게 말하지도 않는다(버튼은 사용자가 누른다).

### 쇼핑 — 찾아 와서 견줘 주기 (`shop_drive`, D68)
폰의 앱 안 브라우저를 맥의 작업이 **멀리 있는 브라우저**로 쓴다. 맥은 쇼핑 사이트에 접속하지 않는다. 폰은 열고 읽기만 한다(누르지 않는다).
- 켜지는 조건: health `features`에 `shop_drive`가 있고, 요청의 `client_features`에 `"shop"`과 `"shop_drive"`가 둘 다 있을 때. 아니면 위의 1단계(`action shop`) 그대로.
- 살 것을 찾는 말 → `action: {"type": "task", "task": {…}}`(제목 "쇼핑 화면에서 찾기") + 말풍선 1개. 앱은 평소처럼 작업 상태를 본다.
- 작업이 `running`인 동안 상태에 `browser`가 실릴 수 있다 — **폰이 할 일 하나**다. 맥은 주소를 주지 않는다.
  - `{"id": "b1", "type": "search", "query": "생수 2L 6개"}` — 쇼핑 검색 화면을 열고 목록을 읽는다.
  - `{"id": "b2", "type": "open", "items": [0, 3]}` — 방금 읽은 목록의 그 차례(0부터, 서로 다른 정수, 1~3개, 0 ≤ i < 8) 상품 화면을 열고 읽는다.
- 앱은 한 일을 `POST /v1/tasks/{id}/browser`로 돌려준다. 응답은 작업 상태(200).
```json
{"id": "b2", "result": "ok", "status": 200, "reason": "",
 "views": [{"kind": "product", "item": 0, "url": "https://…", "fields": {"title": "…", "price": 4900, "seller": "…", "shipping": "3,000원", "options": [], "arrival": "…"}}],
 "skipped": [{"item": 3, "reason": "not_product"}]}
```
  - `result`: `ok` | `nothing`(검색 결과 없음) | `limited`(사이트가 제한 — `status`에 HTTP 코드) | `blocked`(로그인·확인 화면으로 넘어감) | `failed`(열지 못함) | `refused`(앱이 하지 않음).
  - `skipped[].reason`(앱이 보내는 값): `outside` | `no_link` | `not_product` | `unread` | `failed` | `limit`, 빌드 47부터 `no_anchor` | `no_href` | `other_mall`. 맥은 기록용으로만 받는다(판단에 쓰지 않는다).
  - 목록 줄의 선택 키 `can_open`(bool, 빌드 47부터 — 맥이 시킨 `search`의 목록에서만, 모든 줄에): 앱이 그 줄에서 바로 여는 상품 주소를 찾았는가. 하나라도 오면 맥은 `true`인 줄 가운데서만 열 것을 고르고, 전부 `false`면 `open`을 싣지 않고 목록 글로 끝낸다. 키가 없으면(옛 앱) 예전처럼 읽힌 줄 모두에서 고른다.
  - `search`가 `failed`일 때 `reason`(빌드 47부터): `load_error` | `net_lost` | `net_timeout` | `net_offline` | `web_crashed` | `http_status`(`status`에 응답 번호) | `timeout`. 맥은 기록만 하고 다시 싣지 않는다(자동 재시도 없음).
  - 맥의 서버 기록에는 결과마다 꼴만 한 줄 남는다(결과 낱말·`status`·`reason`·view 개수·`skipped` 이유별 수). `reason`과 `skipped[].reason`은 소문자·밑줄 20자 이하일 때만 그대로 적고 그 밖은 `other`로 적는다 — 상품 이름·가격·주소는 적지 않는다.
  - `views`: 1단계 `view`와 같은 꼴(`product` | `list`)에 `item`(목록에서의 차례 — 상품 view에만)이 붙는다. 맥은 같은 검사로 아는 칸만 쓴다. 검색의 답은 `list` 하나, 열기의 답은 `product` 여러 개.
  - `id`가 지금 맡긴 일이 아니면 **409 `conflict`**(앱은 조용히 넘긴다). 바로 앞 일의 결과가 다시 온 것은 200으로 받고 처음 것만 쓴다. 끝난 작업에 온 새 결과도 409. `id`가 없으면 400.
- `limited`·`blocked`를 받으면 맥은 다음 일을 싣지 않고 끝낸다: 읽은 것이 없으면 `failed` + `outcome: "site_blocked"`(`report.blocked` 첫 줄에 이유), 목록까지 읽었으면 `completed` + 목록 글 + `report.blocked`에 이유.
**사이트 안에서 하기 (`site_do`, D72).** 사용자가 주소로 로그인해 둔 사이트(공통 로그인) 안에서 말한 일을 한다.
- 시작: 대화 응답이 `action: {"type": "task", "task": {…, "skill": "site_do", "title": "example.com에서 하기"}}`. 그 사이트에 로그인이 안 되어 있으면 대신 `action: {"type": "site_login", "site": "example-com", "resume": true}` + "로그인이 끝나면 이어서 할게요" — 로그인 뒤 앱이 그 말을 다시 보내거나 사용자가 "했어"라고 하면 잇는다(사이트 읽기와 같다).
- **허락 질문**: 그 사이트를 처음 다룰 때 작업이 `awaiting_input`으로 멈춘다 — `question.text` "example.com의 로그인한 화면을 Claude가 보고 조작해도 될까요? …", `question.choices: ["이 사이트는 허락", "이번만", "하지 않기"]`. 고른 글을 그대로 `POST /v1/tasks/{id}/answer`로 보낸다. "이 사이트는 허락"이면 서버가 기억한다(다음부터 묻지 않는다). 답이 없으면 하지 않고 끝난다(`completed`, 하지 않았다는 글).
- **승인 카드**: 무엇을 바꾸는 단추(저장·추가·삭제·제출·로그아웃·결제·주문 …)를 누르기 전에 `awaiting_approval` — `approval.kind: "action"`, `approval.action`은 누를 단추의 글(예 "추가", "주간 보고 삭제"), `approval.fields`는 그때 화면의 입력값(예 `{"새 할 일": "우유 사기"}`), `approval.url`은 그 화면의 주소. 승인·거절은 초안과 같은 방식(`POST …/approval`, `sha256`). 거절하면 작업은 `failed` + `outcome: "approval_denied"`.
- 결과: `completed`, `report.done`(한 일과 화면에서 읽은 값), `report.private: true`(로그인한 화면에서 읽은 것), `report.sources`는 **그 사이트의 첫 주소 하나**(`https://호스트/` — 로그인한 화면의 주소는 싣지 않는다, 2026-11-15부터), `report.by: "claude"`, 화면 캡처(`report.attachments`, 켜져 있으면). 승인하고 누른 단추는 `result.approved_actions: ["추가"]`로 온다 — **`approved_drafts`에는 넣지 않는다**(초안이 아니다).
- 로그인이 풀려 있으면: `failed` + `outcome: "needs_login"`, `result.action: {"type": "site_login", "site": …}`(사이트 읽기와 같다).
- `GET /v1/sites`의 공통 로그인 사이트에는 `inside: true`(이 길로 다룰 수 있다)와 `cloud_ok`(허락해 두었는가)가 붙는다.
- 탈퇴·비밀번호 바꾸기는 승인 카드도 내지 않고 하지 않는다.

**쇼핑 — 검색 API로 찾아 카드로 (`shop_api`, D71) — 지금은 꺼져 있다.** 쓰려던 네이버 쇼핑 검색 API가 2026-07-31에 종료된 것을 뒤늦게 확인했다(D71 고침). health에 `shop_api`가 없고, 이 형식의 작업은 오지 않는다. 폰의 쇼핑 화면 길도 꺼져 있다(health에 `shop_drive`·`shop_view` 없음) — 살 것을 찾는 말은 지금 웹 검색 길(`web_lookup`·`web_find`)로 답한다. 아래 형식은 다른 상품 검색 원천이 정해지면 그대로 쓴다.
- 응답은 폰 길과 같다: `action: {"type": "task", "task": {…, "skill": "shop_api", "title": "쇼핑에서 찾기"}}` → 몇 초 뒤 `completed`, `result.report.done`(견준 말 2~4줄, `by`), `result.report.items`(최대 3장).
- 카드: `{"id": "i1", "title", "url", "price", "mall"?, "image"?, "detail"?}`. `image`는 상품 그림 주소(https, 새 키). `detail`은 "가격비교 최저가"일 수 있다(그 값은 여러 판매처 가운데 최저가다). **`shipping`은 오지 않는다** — 배송비·옵션은 API에 없다.
- `url`은 쇼핑 검색 API가 준 상품 주소다. **앱 안 쇼핑 화면으로 열면 사이트의 확인(보안문자)을 다시 만날 수 있다 — 기기의 브라우저(Safari)나 쇼핑 앱으로 여는 것을 권한다.**
- 못 했을 때: `failed` + `outcome: "search_failed"`(새 이름 — `provider_error`는 앱이 '맥의 모델을 부르지 못했다'로 보여 줘서 쓰지 않는다. 모르는 outcome으로 다뤄도 된다: 일반 실패 문장 + `report.blocked`의 이유), `result.reason`: `not_set`(키 없음) | `auth`(키·권한) | `limited`(하루 한도) | `unreachable` | `bad_response`. `report.blocked` 첫 줄에 사람이 읽는 이유.
- 결과가 없거나 조건에 맞는 것이 없으면 `completed`이고 `items` 없이 글만 온다.
- 카드를 가리킨 답장(`reply_to.item_id`)과 그 뒤의 물음은 폰 길과 같다.

- 폰이 5분 안에 답하지 않으면 `failed` + `outcome: "timeout"`(`report.blocked` 첫 줄 "폰의 쇼핑 화면이 응답하지 않았어요 …"). 폰이 열지 못했거나 꼴이 다른 결과면 `failed` + `outcome: "browser_failed"`. 취소하면 기다림이 바로 끝난다.
- 끝나면 `completed` + `result.report`:
```json
{"done": ["1번은 6개 묶음이 4,900원에 배송비 3,000원이 따로 붙어요. …"], "blocked": [], "sources": [], "by": "claude",
 "items": [{"id": "i1", "title": "…", "url": "https://…", "price": 4900, "mall": "…", "shipping": "3,000원", "detail": "수량: 1묶음, 2묶음 · 내일 도착 예정"}]}
```
  - `items`(상품 카드, 3장까지): **값은 폰이 읽은 칸 그대로**다(코드가 옮긴다). `title`·`url`은 항상, `price`(JSON 숫자)·`mall`·`shipping`·`detail`은 읽혔을 때만. `url`은 폰이 보낸 상품 화면 주소 그대로 — 카드를 누르면 앱이 그 화면을 연다.
  - `done`: 사용자의 조건과 견준 말 2~4줄(`by: "claude"`). 재료에 없는 숫자(합계 등)가 든 말은 코드가 버리고 정해 둔 문장을 쓴다(그때는 `by` 없음 = 맥). 상품 화면을 못 열었으면 `items` 없이 목록 글만 `done`에 온다.
- 카드를 가리킨 답장: 요청의 `reply_to: {"task_id": "…", "item_id": "i1"}`. 그 작업의 카드일 때만 가리킨 것으로 친다(꼴이 다르면 없는 것으로 본다).
- 맥의 답은 주문·결제·장바구니 담기를 해 주겠다고 하지 않는다("주문해줘"에는 카드를 눌러 상품 화면에서 직접 하시라고 답한다).
- Claude가 보는 것: 검색어, 사용자의 그 말, 목록·상품의 이름·가격·판매처·배송·옵션(앞의 2묶음, 값 4개까지)·도착 안내(공개 상품 정보). 그 뒤의 대화 기록에도 같은 칸이 남는다.
  **상품 주소는 어느 호출에도 보내지 않는다**(고르기·견주기·그 뒤의 대화 모두 — 주소는 카드에만 있다). 재고(`stock`)·정가·회원가는 이 길에서 맥이 쓰지 않는다. 주문서·결제·내 정보 화면의 값은 이 길에 들어오지 않는다(`product`·`list`가 아닌 것은 버린다).

### 반응 이모지 (`reaction`, D64)
메신저에서 상대의 말풍선에 다는 반응처럼, Claude가 사용자의 이번 말에 이모지 하나를 달 수 있다.
- 요청의 `client_features`에 `"reaction"`이 있을 때만(앱이 그릴 수 있을 때). 없으면 응답은 지금과 똑같다.
- 응답의 선택 키 `"reaction": "👌"` — `/v1/messages` 응답의 맨 위에 실린다. 고르지 않았으면 키가 없다. 값은 `👍 ❤️ 😂 😮 😢 🙏 👌 🎉` 중 하나(맥이 코드로 거른다).
- 짧은 맞장구·고마움·기쁜 소식·농담 같은 말에만 달고, 일을 맡기는 말·물음에는 달지 않는다.
- **반응만 있는 응답**: `client_features`에 `reaction`을 보낸 요청에는 `action: {"type":"none"}`에 `replies`가 **빈 배열**이고 `reaction`만 있을 수 있다("ㅇㅋ", "넵"). 그 밖의 요청에서는 전처럼 `none`의 `replies`가 1개 이상이다.
- 로컬 판단으로 처리한 말에는 반응이 없다. 같은 `client_message_id`로 다시 온 요청은 같은 반응을 돌려준다.
```json
{"conversation_id": "c_…", "replies": [{"text": "별거 아니에요 ㅎㅎ", "by": "claude"}], "action": {"type": "none"}, "reaction": "🙏"}
```

### 푸시 알림 (`push`, D44)
앱이 뒤에 있을 때 작업의 상태 변화를 Apple 푸시로 알린다. `features`에 `push`가 있을 때만 아래 경로가 있다(없으면 `404 not_found`).
- `POST /v1/push/devices` `{"token": "<16진수 64~200자>", "environment": "production" | "sandbox", "bundle_id"?: "…"}` → `200 {"registered": true}`.
  같은 토큰은 덮어쓴다. 기기는 10개까지(넘으면 가장 오래 쓰지 않은 등록을 지운다 — 오류 아님). 등록은 서버를 다시 띄워도 남는다.
  `400 invalid_request`: 토큰 꼴, environment 값, **`sandbox`(지원하지 않는다)**, `bundle_id`가 맥 설정과 다름.
- `DELETE /v1/push/devices/{token}` → `200 {"registered": false}`(없는 토큰이어도 200).
- 알림의 내용 — 고정 문장과 종류·id뿐이다. 작업 제목·초안·받는 사람·사이트 이름·읽은 결과는 넣지 않는다. 내용은 앱이 열린 뒤 `GET /v1/tasks/{id}`로 받는다.
```json
{"aps": {"alert": {"body": "승인이 필요한 일이 있어요"}, "sound": "default", "thread-id": "<task_id>"},
 "type": "approval_needed", "task_id": "<task_id>", "conversation_id": "c_…"}
```
| `type` | 언제 | 문장 |
|---|---|---|
| `approval_needed` | 작업이 승인을 기다린다(`awaiting_approval`). 새 초안이 오면 다시 알린다 | 승인이 필요한 일이 있어요 |
| `input_needed` | 작업 담당의 질문(`awaiting_input`), 또는 확인이 필요해 멈춤(`needs_input`) | 확인이 필요한 질문이 있어요 |
| `task_done` | 완료 | 맡긴 일이 끝났어요 |
| `task_failed` | 실패 | 맡긴 일을 끝내지 못했어요 |
| `needs_login` | 로그인이 없거나 풀려 멈춤 | 로그인이 필요해서 멈췄어요 |
| `routine_result` | 예약해 둔 일이 돌아 답이 바로 나왔다(D80). `task_id`는 실행 id(`rr_…`) | 루틴 결과가 도착했어요 |

- **알림에 띄울 실제 글**(features `push_text`): Apple을 거치는 것은 위의 고정 문장과 id뿐이고, `aps`에 `"mutable-content": 1`이 붙는다.
  앱의 알림 확장이 `GET /v1/tasks/{task_id}/notification?type=<푸시의 type>`으로 글을 맥에서 직접 받아 바꿔 띄운다.
  `200 {"type", "title": 작업 제목, "body": 띄울 글(300자까지, 줄바꿈 있음), "private": 로그인한 화면에서 읽은 개인 내용이 든 글인가}`.
  body — `approval_needed`: "메일을 보내도 될까요? 제목: …"(본문·받는 사람 없음) / `input_needed`: 질문 글 / `task_done`: 결과의 답 앞부분 / `task_failed`·`needs_login`: 못 한 까닭 첫 줄.
  `404 notification_gone`(그 type에 띄울 글이 지금은 없다)·`404 task_not_found`(서버 재시작 등)면 고정 문장을 그대로 둔다. `400 invalid_request`: type이 없거나 모르는 값.
- 같은 일로는 한 번만 보낸다. 사용자가 취소한 작업, 10초짜리 발송 대기, 대화 답(`action none`)에는 보내지 않는다. `conversation_id`는 대화에서 만든 작업일 때만 있다.
- 한 작업의 알림은 최신 것 하나로 바뀐다. 승인·질문 알림은 그 만료 시각(30분)이 지나면 전달되지 않는다.
- 앱이 앞에 떠 있어도 서버는 구분 없이 보낸다(숨길지는 앱이 정한다). Apple이 없는 기기라고 답하면 서버가 그 등록을 지운다.

### 사이트를 직접 열어 보기 (`web_browse`, D67)
`features`에 `web_browse`가 있으면, 정해진 기능에 없는 부탁 가운데 공개 사이트에서 직접 열어 보면 되는 일("○○ 홈페이지에서 … 봐줘", 조회 화면에서 골라야 아는 것)은 작업이 된다.
- 응답: `action.type: "task"` + 말풍선 1개. 작업 제목은 "웹에서 직접 해 보기". 진행 글(narration)과 단계(steps)는 다른 작업과 같다(도구 이름: navigate·observe·click·type·select_option·scroll·extract_text·search_web·ask_user·finish).
- 맥이 하는 일: **로그인 상태가 없는 일회용 격리 브라우저**로 사이트를 열고, 검색칸에 입력하고, 누르고, 읽는다. 화면(글)을 보고 다음 행동을 고르는 것은 Claude다. 측정 4~27초가 대부분, 길면 3분(한도).
- 결과(`result.report`): `done` — 답(원소 하나), `sources` — 실제로 연 페이지, `links` — 마지막으로 읽은 페이지 0~1개(제목·주소·사이트·대표 그림), `by: "claude"`, 캡처가 켜져 있으면 마지막 화면 한 장(`sensitive: false`). `private`는 없다.
- 하지 않는 것: 로그인, 신청·결제·예약·제출 같은 바꾸는 동작(그런 버튼은 누르지 않고 `report.blocked`에 "… 바꾸는 동작은 하지 않았어요"), 검색 사이트·네이버 열기.
- 실패 경로: 사이트가 막으면 그 사이트로 더 가지 않고 `report.blocked`에 "○○가 이 브라우저의 접근을 막았어요". 사이트에서 직접 답을 못 얻으면(막힘·조작 실패·한도) 검색이 쓴 답으로 대신하고 첫 줄에
  "사이트에서 직접 확인하지는 못했어요. 검색 결과로는 이렇게 나와요."라고 밝힌다(`links` 없음). 검색도 못 하면 못 했다는 답 그대로. 작업 담당이 사용자에게 물어야 하면 질문 카드(`awaiting_input`).
- 나가는 것: 이 작업에서 맥의 브라우저가 연 **공개 페이지의 화면 글(주소·제목·요소 이름·본문 글)이 Claude로 간다**(그림·캡처는 가지 않는다). 운전하는 Claude는 할 일 한 문장(대화 판단이 쓴 것, 메일 주소는 뺀다)과 지금 시각만 받는다 — 대화 기록·연락처·회의 자료는 받지 않는다.
  맥이 사이트의 검색칸·입력칸에 넣는 글은 Claude가 그 문장에서 쓴 것이다(사용자가 말한 낱말이 그 사이트로 갈 수 있다). 끝난 답과 링크의 제목은 대화 기록에 남아 다음 판단 때 Claude가 본다. 화면 글은 맥에 저장하지 않는다.

### 날씨는 앱이 보여 준다 (`weather_action`, D62)
맥은 "날씨를 묻는 말이고 어디·언제"까지만 정하고, 날씨는 앱이 자기 데이터와 위치로 보여 준다. 위치는 폰 밖으로 나오지 않는다.
- 요청의 선택 키 `client_features` — 앱이 할 수 있는 일의 이름 목록. 지금 뜻이 있는 값은 `"weather"`(날씨 카드를 그릴 수 있다). 없거나 꼴이 다르면 지금과 똑같이 동작한다(날씨 물음은 웹 검색 답).
- 응답: `action: {"type": "weather", "place": "부산", "when": "tomorrow"}` + 말풍선 1개. 작업은 만들지 않는다.
  `place`는 사용자가 지역을 말했을 때만 있다(없으면 사용자의 지금 위치). `when`: `now` | `today` | `tomorrow` | `week` | `YYYY-MM-DD`.
- 요청의 선택 키 `events` — 앱이 지난 응답 뒤에 한 일: `[{"type": "weather", "text": "부산 내일: 흐리고 비, 12~18도, 강수확률 70%"}]`(최대 3개, text 300자).
  대화 기록에 남아 다음 말("우산 챙길까?")을 이어 받는 데 쓰이고, **다음 판단 때 Claude로 간다** — 지금 위치의 날씨는 지명 없이 적는 것을 권한다.
  같은 `client_message_id`로 다시 온 요청의 events는 다시 적지 않는다(id가 없으면 구분하지 못한다). 모르는 `type`은 무시한다.
- 미세먼지·황사·태풍 소식은 이 action이 아니라 웹 검색 답으로 간다.

### 공개 정보 직접 찾기 (`web_find`, D61)
`features`에 `web_find`가 있으면, 사용자가 무엇을 **찾아 달라**고 한 말(가격·살 곳·예매처·공식 페이지·링크)은 작업이 된다. 값만 묻는 말(날씨·환율)은 위의 빠른 답 그대로다.
- 응답: `action.type: "task"` + 말풍선 1개(사이트 읽기와 같은 꼴). 작업 제목은 "웹에서 찾기".
- 맥이 하는 일: 후보 페이지를 찾고(웹 검색 또는 네이버 검색 API) → **로그인 상태가 없는 일회용 브라우저**로 그 공개 페이지를 직접 연다(읽기만, 누르기·입력 없음) → 답과 함께 돌려준다. 측정 10~23초.
- 결과(`result.report`): `done` — 답(원소 하나, 줄바꿈이 있는 한 덩어리 글. 주소는 적지 않는다), `sources` — 실제로 연 페이지 주소(기존과 같음),
  `links` — 실제로 연 페이지의 카드 재료, `by` — 답을 쓴 곳(`claude` | `local`). `private`는 붙지 않는다(공개 정보). 캡처(`attachments`)가 있으면 연 페이지 한 장이고 `sensitive: false`.
```json
"links": [{"title": "관람 안내", "url": "https://www.museum.example/guide", "site": "museum.example", "image": "https://www.museum.example/og.jpg"}]
```
  - 최대 5개(지금은 3개까지). 맥의 브라우저가 실제로 연 페이지만 — 모델이 쓴 주소는 넣지 않는다. `url`은 https의 공개 도메인만, 500자 이하. `title`은 페이지가 적어 둔 제목(없으면 검색이 준 제목·도메인), 80자까지.
  - `image`(선택): 그 페이지가 대표 그림으로 적어 둔 주소(og:image). https의 공개 도메인일 때만 싣는다. 맥은 그림을 내려받지 않는다 — 페이지가 적은 값이라 깨졌거나 엉뚱할 수 있다.
  - 페이지를 하나도 열지 못했으면 `links`가 없다(답은 검색 결과로 쓴 것).
- 실패: 찾지 못하면 `status: "failed"`, `report.blocked`에 이유. 지어낸 답은 내지 않는다.
- 나가는 것: 검색할 물음 한 문장(웹 검색과 같음. `web_find_naver`면 네이버로 간다). 맥의 브라우저가 연 페이지의 글은 Claude로 가지 않는다. 끝난 답은 대화 기록에 남아 다음 판단 때 Claude가 본다.

### `POST /v1/tasks/{id}/answer` — 작업 담당의 질문에 답 (v2)
```json
{"question_id": "Qx8mK2pA", "answer": "3분기"}
```
| 필드 | 형식 | 설명 |
|---|---|---|
| `question_id` | 문자열, 필수 | `task.question.question_id`. 다르면 409 |
| `answer` | 문자열 1~1000자, 필수 | 선택지를 골랐으면 그 문자열, 아니면 사용자가 쓴 답 |

응답 `200`: 작업 객체. 승인과 마찬가지로 직후 응답은 아직 `awaiting_input`이고 `question.received: true`일 수 있다. 같은 질문에 두 번 답하면 409.
막힌 질문은 대화 화면에 질문 카드로 보여 주는 것을 권장한다(선택지는 버튼). `/v1/messages`로 보내도 질문의 답으로 전달되지 않는다.

### `POST /v1/tasks/{id}/cancel` — 취소
응답 `202`: 작업 객체. 실제 중단은 진행 중인 단계가 끝난 뒤라 잠시 `running`으로 보일 수 있다. 이미 끝난 작업이면 409. `send_pending` 중에 취소하면 메일을 보내지 않는다. 대기가 끝나 보내기 시작한 뒤에는 409 `conflict`("이미 발송했다") — 작업을 다시 조회해 `report.sent`를 보여 준다.

### `GET /v1/tasks` — 최근 작업 목록
응답 `200`: `{"tasks": [{"id", "status", "task", "title", "conversation_id"?, "created_at", "updated_at"}, …]}` 최신순, 최대 20개.
`?conversation_id=c_…`를 붙이면 그 대화에서 만든 작업만 돌려준다(v2) — `/v1/messages` 응답을 받지 못했을 때(시간 초과 등) 만들어진 작업을 찾는 데 쓴다.

### 오래 가는 기억 — `/v1/memory` (D129, features `memory`)
비서가 사용자에 대해 '항상 쥐고 있는 요약본'(마크다운)과 옛 대화 보관함. 요약본은 대화 판단에 매 말마다 함께 가고, 대화가 15분 잠잠하면(“기억해줘”·“잊어줘”가 있었으면 1분) 맥이 한 번에 고쳐 쓴다.
비밀번호·카드·계좌·주민번호·인증 코드 꼴은 맥이 지우고 넣는다. 사이트에서 읽은 내용·받은 메일 내용은 대화 기록에 이름표만 있으므로 여기에도 없다.
- `GET /v1/memory` → `200 {"profile": "## 나\n- …", "turns": 123, "since": "2026-11-18"|null, "profile_updated_at": "…"|null, "profile_by": "digest"|"user"|"undo"|null}`
- `PUT /v1/memory/profile` 본문 `{"profile": "<마크다운, 6000자 이하>"}` → `200 {"profile": "<저장된 글 — 비밀 꼴은 지워지고 3000자로 잘린다>"}` (사용자가 손으로 고치기)
- `POST /v1/memory/undo` → `200 {"profile": "…"}` 바로 전 요약본으로 한 단계 되돌리기(다시 부르면 둘 사이를 오간다). 전 것이 없으면 `409 nothing_to_undo`
- `GET /v1/memory/search?q=<1~100자>` → `200 {"hits": [{"when": "2026-11-11 20:10", "role": "user"|"assistant", "text": "…", "next": "바로 뒤 상대의 말 또는 ''"}]}`
- `DELETE /v1/memory` → `200 {"ok": true}` 요약본·보관함 모두 지우기(되돌릴 수 없다 — 앱이 확인을 받고 부른다)
- 대화에서: “기억해줘 …”·“… 잊어줘”는 말로 된다. “지난주에 얘기한 … 뭐였지?”는 맥이 보관함에서 찾아 답한다(못 찾으면 못 찾았다고 한다).

## 오류
모든 오류 응답: `{"error": {"code": "<코드>", "message": "<한국어 설명>"}}`

| HTTP | code | 뜻 |
|---|---|---|
| 400 | `invalid_json` | 본문이 JSON이 아님 |
| 400 | `invalid_request` | 필드 누락·형식·길이 오류 |
| 401 | `unauthorized` | 토큰 헤더가 없거나 틀림 |
| 404 | `not_found` | 없는 경로 |
| 404 | `task_not_found` | 없는 작업 ID (서버 재시작 포함) |
| 405 | `method_not_allowed` | 그 경로에서 지원하지 않는 메서드 |
| 409 | `conflict` | 승인(질문) 대기 중이 아님 / `approval_id`·`question_id` 불일치 / 이미 응답한 승인 요청·질문 / 이미 끝난 작업 취소 |
| 403 | `not_allowed` / `too_many_accounts` | `mail/accounts/connect`: 지원하지 않는 메일 서비스 / 계정 수 한도. `disconnect`: 연결되지 않은 주소 |
| 400 | `provider_mismatch` | `mail/accounts/connect`: 고른 서비스와 주소 도메인이 다름 |
| 429 | `rate_limited` | `mail/accounts/connect`: 주소당 10분에 5번, 전체 10분에 15번을 넘음 (`busy`와 code로 구분) |
| 404 | `unknown_site` / `unknown_login` | 사이트 로그인: 지원하지 않는 사이트 / 없는(오래된) 로그인 |
| 409 | `stale_step` / `busy` | 사이트 로그인: 지난 단계의 입력 / 로그인 진행 중 |
| 409 | `hash_mismatch` | 승인 요청의 `sha256`이 고정한 초안의 해시와 다름. 실행하지 않음. 작업을 다시 조회해 최신 승인 카드를 보여 준다 |
| 413 | `payload_too_large` | 본문 64KB 초과 |
| 429 | `busy` | 대기 중인 작업이 많음(실행 1 + 대기 5 초과). 잠시 후 재시도 |
| 429 | `too_many_auth_failures` | 60초 안에 인증 실패 10회. 60초 뒤 재시도 |
| 500 | `internal_error` | 서버 내부 오류 |
| 502 | `model_error` | (`/v1/messages`) 로컬 모델 호출 실패. 잠시 후 재시도 |

## 앱 쪽 흐름 예
1. `POST /v1/tasks` → `id` 저장
2. `GET /v1/tasks/{id}` 반복 → `steps`로 진행 표시
3. `awaiting_approval`이면 `approval.fields`를 원문 그대로 보여 주고 사용자가 선택 → `POST …/approval` (승인이면 `sha256` 포함). `approval.received`가 있으면 처리 중으로 표시
4. 거부 사유를 보냈다면 다시 2번으로 (새 `approval_id`로 새 초안이 온다)
5. 종료 상태면 `result` 표시. `needs_input`이면 질문을 보여 주고 답을 넣어 새 작업 요청

## 변경 이력
- 2026-11-18 (D117): 통화 목소리. 대화 판단이 Claude이고 맥에서 `VOICE_TTS=1`일 때 health에 `voice_tts`가 나온다. 그때 받는 것은 말풍선의 `replies[].voice_id`와 `/v1/voice` 경로다(`POST /v1/voice/warm`, `POST /v1/voice`, `GET /v1/voice/{id}/{n}`, `DELETE /v1/voice/{id}`). 앱 쪽 순서: 받은 조각을 차례로 재생한다. 처음 쓸 때는 첫 조각까지 7~8초, 데운 뒤에는 약 0.8초다. 늦거나 503이면 폰 목소리로 읽는다.
- 2026-11-17 (D116): 에이전트에게 거는 전화. 앱은 폰에서 듣고(기기 안 음성 인식) 폰에서 읽는다. 맥이 받는 것은 채팅과 같은 글이다. 요청 `client_features`에 `"voice_call"`이 있으면 다음처럼 된다.
  - 통화 말투 규칙이 붙는다.
  - 응답 `replies[].speak`가 붙는다. 이모지·마크다운·링크를 뺀 읽을 글이다.
  - 모든 끝난 작업의 `result.report.speak`에 한두 문장(160자 이하)이 실린다. 카드를 만든 쪽이 고른 숫자로 쓴다.
  - 통화도 지금 채팅의 `conversation_id`·`context`·`contacts`를 그대로 보내면 같은 흐름을 안다.
  - 맥은 통화 내용을 파일에 남기지 않는다(대화 기록은 원래대로 메모리 24시간).
  - `route_claude`(회의 답변)는 앱이 답하는 길이니, 통화 중에는 앱이 그 답도 짧은 말투로 받아야 한다.
- 2026-11-17 (D112): 간편인증에서 "인증 완료"를 고른 뒤 인증 기관이 확인하는 동안 로그인 상태가 `need_approval`(message "간편인증을 확인하고 있어요 …", `host`는 간편인증 호스트)일 수 있다 — 새 키 없음. 그 뒤 "인증 완료" 카드가 다시 오면 `text`는 "아직 인증이 확인되지 않았어요."로 시작한다.
- 2026-11-17 (D110): `GET /v1/sites`의 줄에 `host`(그 사이트의 대표 호스트, 소문자 — 로그인돼 있어야 열리는 화면의 것). `GET /v1/sites/{site}/icon`(맥이 로그인 확인 때 그 화면에서 받아 둔 아이콘, 64px PNG, 없으면 404 `icon_not_found`). 아이콘은 로그인·로그인 확인(6시간마다) 때 차례로 채워진다.
- 2026-11-17: 결과 화면 캡처(`report.attachments`)가 페이지가 창보다 길면 창 높이씩 나눠 3장까지 — 캡션 "○○ 화면 (1/3)". 작업당 첨부는 4개까지(전에는 2). 승인·질문 카드의 그때 화면은 그대로 한 장.
- 2026-11-17 (D108): 요청의 `reply_to`에 선택 키 `text`(답장한 말풍선의 글, 500자까지)·`role`(`"user"`|`"assistant"`) — 작업이 아닌 말에 답장해도 그 말을 가리킨다. 이름만 말한 모르는 사이트의 로그인은 `action: ask`에 `choices` `["jobkorea.co.kr 로그인", …, "여기 없어요 — 주소를 알려 줄게요"]`(고르면 그 글을 그대로 보내면 된다).
- 2026-11-17 (D106): 손 넘기기 — features `handover`, `question.handover`, `GET /v1/handover/{id}/frame`, `POST /v1/handover/{id}/input`.
- 2026-11-17 (D105): `GET /v1/sites`의 줄에 `via`(`"password"` 또는 소셜 회사 키 — 처음 로그인한 방법, 있을 때만)·`backend: "chrome"`(전용 크롬 사이트만). 알림 종류 `login_expired`(몇 시간마다 하는 살려 두기 점검에서 풀린 것을 알았다 — payload에 `site`, `task_id`는 `"site:<키>"`, 같은 날 같은 사이트는 한 번).
- 2026-11-17 (앱 D119에 맞춤): 작업 결과의 긴 답(점검·비교처럼 갈래가 여럿)은 `## 소제목` 줄(앞에 이모지 하나 가능)과 그 아래 `- ` 줄로 올 수 있다. 짧은 답·대화 말풍선은 그대로. 표·번호 목록은 쓰지 않는다. 카드 글에서는 `#`이 빠진다.
- 2026-11-17 (D103): 활동·전역 검색 — features `activity`, `GET /v1/activity`, `GET /v1/search?q=`.
- 2026-11-17 (D102): 실패 카드(`report.card.kind: "failed"`)에 `subtitle` — 멈춘 까닭 한 줄(까닭 글 → 요약 → 결과 종류 순, 제목과 겹치는 앞부분은 뺀다). 로그인이 필요한 카드에는 없다(단추가 할 일). 사람 확인 화면에서 멈춘 작업은 `outcome: "site_blocked"`, `reason: "challenge"`.
- 2026-11-16 (D80): 예약해 둔 일 — features `routines`, `GET·POST /v1/routines`, `POST·DELETE /v1/routines/{id}`, `GET /v1/routines/runs`, 대화 응답의 `routines`(예약을 만들거나 바꾼 말의 답에만), 알림 `routine_result`를 쓰기 시작.
- 2026-11-16 (D79): 로그인 카드에 간편인증 — `need_input`의 칸 이름 `name`·`birth`·`phone`(모두 `secret: false`), `need_choice`의 고르는 말이 14개까지(인증 앱), `text`에 줄바꿈이 든 동의 목록과 고르는 말 "동의하고 인증 요청", 폰 승인 뒤 누르는 "인증 완료". 형식은 그대로. `send_pending`의 `send`에도 `external: true`(D77).
- 2026-11-16 (D78): `POST /v1/messages`의 `contacts`를 500명까지 받는다(전에는 50). `GET /v1/sites`의 사이트에 `aliases`(사용자가 부르던 이름, 있을 때만).
- 2026-11-16 (D77): 다른 사람에게 메일 보내기 — features `send_others`. 형식은 본인 발송과 같다(승인 `kind: "send"`, `send_pending`, `report.sent`). 달라지는 것: 받는 사람이 내가 아니면 `approval.external: true`, `fields["보내는 사람"]`≠`fields["받는 사람"]`, `report.sent[].external: true`이고 `verified`는 늘 `accepted_only`. 여러 명이면 `ask`("누구에게 먼저 보낼까요?").
- 2026-11-15 (D72): 사이트 안에서 하기 — features `site_do`, 작업 `skill: "site_do"`, 승인 `kind: "action"`, 결과 `approved_actions`, `/v1/sites`의 `inside`·`cloud_ok`. 질문 카드로 사이트별 허락을 묻는다.
- 2026-11-15 (D70 이어서 2): 소셜 로그인 중간 화면의 단추를 묻는 `need_choice`(고르는 말에 "기다리기"), 로그인 시작 한도 10분에 12번. 본문을 읽지 않는 경로에 본문을 보내도 다음 요청이 깨지지 않는다.
- 2026-11-15 (D70 이어서): 대화에서 주소로 로그인 시작(`site`가 주소에서 만든 키), 로그인 방법을 묻는 `need_choice`, 공통 로그인의 `need_approval`. 새 상태·새 키 없음.
- 2026-11-15 (D71 고침): `shop_api`는 켜지 않는다 — 네이버 쇼핑 검색 API가 2026-07-31에 종료됐다. health에 `shop_api`·`shop_drive`·`shop_view` 모두 없음.
- 2026-11-15 (D71): 쇼핑을 공식 검색 API로 — features `shop_api`, 작업 `skill: "shop_api"`, 카드의 `image`, `outcome: "search_failed"`와 `reason`. 서버 스위치로 폰의 쇼핑 화면 길(`shop_drive`·`shop_view`)을 껐다(health에서 사라짐).
- 2026-11-15: 쇼핑 목록 줄의 선택 키 `can_open`, `search` 실패의 `reason` 낱말, `skipped[].reason`의 새 낱말(앱 빌드 47). 로그인 객체의 선택 키 `host`.
- 2026-11-18 (D106 이어서): 공통 로그인 `need_choice`에 `handover`(작업 질문 카드의 `question.handover`와 같은 꼴 — `id`·`reason`·`host`·`frame`·`input`·`idle_close_s`)가 실릴 수 있다. 글자로 답할 수 없는 확인 문제(퍼즐)가 떴을 때 — 앱은 손 넘기기 화면으로 열고, 다 풀면 `{"choice": "다 했어요"}`.
- 2026-11-18 (D126): 작업 결과의 `action: {"type":"site_login",…}`에 `resume: true`(로그인 뒤 그 부탁을 다시 보내면 잇는다). 공개 길(web_browse)이 부탁에 나온 사이트의 로그인 화면에서 멈추면 `outcome: needs_login` + 그 행동. 로그인한 사이트에서 표를 내려받기 전에 질문 카드("이 사이트는 허락" / "이번만" / "안 돼요"). 새 키 없음.
- 2026-11-18 (D120): 공통 로그인 — 실패 안내 화면의 `need_choice`에 "다시 해 보기" 고르는 말(새 키 없음).
- 2026-11-15 (D70): 공통 로그인 — 네이버가 아닌 사이트도 같은 로그인 형식으로(새 키 없음). 실패 사유 `not_https`·`host_refused`·`unsupported_login`, `need_choice`의 고르는 말("계속"/"그만하기", "로그인됐어요"/"아직이에요"/"그만하기"). 사이트를 맥에 적기 전에는 지금처럼 네이버만 보인다.
- 2026-11-15: 작업 상태에 선택 키 `skill`(어느 길의 작업인지). 기존 키는 그대로.
- 2026-11-14 (D68): 쇼핑 — 찾아 와서 견줘 주기. features `shop_drive`(켰을 때만), 요청 `client_features`에 `"shop_drive"`, 작업 상태 `browser`, `POST /v1/tasks/{id}/browser`(409 `conflict`), `result.report.items`, `reply_to.item_id`, 작업 `outcome`에 `browser_failed` 추가(status `failed`). 모두 더해진 값이라 `shop_drive`를 보내지 않는 앱은 그대로 동작한다.
- 2026-11-17: `GET /v1/tasks?status=awaiting_approval,awaiting_input`(쉼표로 여러 상태) — 지금 사용자를 기다리는 작업만. 맥이 직접 만든 대화(작업실 시험 등)의 승인 카드를 앱이 열 때 찾을 수 있게.
- 2026-11-17 (사용자 지시 "참고 앱처럼 1·2·3 다", 고르기 카드 넓히기):
  - 되묻기(`action.type: "ask"`)의 `choices`가 **6개까지**. `multi: true`(있을 때만)면 여러 개를 함께 고른다 — 앱은 고른 것들을 ", "로 이어 한 말로 보낸다. `choice_details`(있을 때만): `[{"text", "image"?}]` — `choices`와 같은 순서, `image`는 이 대화에서 맥이 실제로 연 공개 페이지의 대표 그림(https)만. 없는 줄은 `image`가 없다.
  - 답 뒤의 **다음 말**: 말풍선으로 답한 응답의 `action`(`type: "none"`)에 `next: {"choices": [2~3개], "recommended"?}`(있을 때만). 고르면 그 글을 사용자가 보낸 말처럼 보내면 된다. 대개는 없다(잡담·짧은 답에는 붙이지 않는다).
  - 답변 카드에도 `card.next: {"choices": [...]}`(있을 때만) — 같은 방식.
- 2026-11-16 (사용자 지시 "답변 카드를 참고 앱처럼 일정하게"): **답변 카드**.
  - 끝난 작업(`completed`·`failed`·`needs_input`)의 `result.report.card`: `{"kind": "answer"|"result"|"failed", "title", "subtitle"?, "blocks"?, "actions": [...]}`. `answer`는 읽어 온 결과(제목 "최근 결제 내역" + 블록), `result`는 무언가 한 뒤(제목 "장바구니에 담았어요" + 부제 + 블록), `failed`는 한 줄 까닭(`title`) + 단추. `blocks`는 내 공간과 같은 블록(`text`·`stat`·`list`·`pairs`·`date`, 3개까지, 목록은 하나). `actions`는 맥의 코드가 정한 것만: `{"label": "사이트에서 보기", "url"}` 또는 `{"label": "다시 로그인", "site"}`. 카드가 있으면 `report.done`의 줄글 대신 카드를 그리면 된다(카드를 못 만들었으면 오지 않는다 — 그때는 예전처럼 `done`). 맥에서만 읽은 결과(정해진 사이트 읽기)는 맥의 로컬 모델이 다듬어 30초쯤 더 걸린다.
  - 되묻기의 `action`에 `recommended`(권하는 후보 — `choices` 가운데 하나, 있을 때만): 고르기 카드에서 '추천' 표시.
  - 말풍선 `replies[]`에 `kind: "status"`가 붙으면 한 줄로 끝난 일(예약했어요 · 매일 오전 8:00, 내 공간을 고친 것): 말풍선 대신 작은 상태 줄로.
- 2026-11-16 (사용자 지시 "과하게 들어간 문장·표기는 빼라"): 공개 정보 웹 검색의 답에서 **"출처: …"·"찾아본 곳: …" 말풍선이 없어지고**, 응답 맨 위의 `sources`(사이트 이름 배열, 있을 때만)로 온다 — 앱이 작은 글씨로 그리거나 빼면 된다. 예약을 만든 답은 "예약했어요. 매일 오전 8:00" 한 줄(주의 문구 없음). 로그인한 사이트 안에서 한 일의 답에 "길: A > B > C"·하지 않은 일·캡처 얘기를 쓰지 않는다.
- 2026-11-13 (D59): 멈춘 부탁 — features `site_login_resume`, 로그인이 없어 못 한 부탁의 안내는 `action: {"type":"site_login","site":…,"resume":true}`. 로그인 뒤 "했어"라고만 해도 그 부탁을 잇고(`action.type: "task"`), 같은 읽기가 돌고 있으면 또 만들지 않는다. 읽은 결과를 가리키는 말("이거 … 내 메일로", "아까 그거 …")은 그 결과로 처리한다. 꼴 변화는 `resume` 키 하나. 같은 날 추가: 요청의 `reply_to`(features `reply_to`), 그리고 서버에서 Claude 판단을 켰을 때만 features `dialog_cloud`·`client_time`("대화 판단이 어디서 되나" 절 — 나가는 것 / 안 나가는 것 표).
- 2026-11-13 (D60): 공개 정보 웹 검색 — features `web_lookup`(켰을 때만). 응답 꼴은 그대로(`none` + 말풍선, 마지막 말풍선이 `출처: …`). features `dialog_context` 추가(회의 자료의 내용이 Claude로 가는지). "Claude로 나가는 것" 표에 검색할 물음 한 줄 추가.
- 2026-11-13 (D60 이어서): features `reply_by` — `replies[].by`(`claude`|`local`|`mac`). 로그인이 없어 멈춘 부탁의 안내 글이 "… 로그인이 끝나면 이어서 할게요"로 바뀜(꼴은 그대로 `site_login` + `resume: true`), 로그인이 필요한 부탁에 되묻지 않고 첫 응답에서 바로 그 안내를 준다. 웹 검색 답의 마지막 줄이 `찾아본 곳: …`일 수 있다. 작업 결과 `reason`에 `invented_date`(원문에 없는 연도가 계속 들어가 보내지 않음, outcome `send_failed`).
- 2026-11-14 (D67): 일반 길 — features `web_browse`, 작업 "웹에서 직접 해 보기"(공개 사이트를 맥의 브라우저가 직접 조작). 응답 꼴은 기존 작업과 같다. 단계의 도구 이름에 `search_web`이 나올 수 있다.
- 2026-11-14 (D66): 쇼핑 1단계 — features `shop_view`, 요청 `client_features: ["shop"]`·`view`, 응답 `action.type: "shop"`. 맥은 쇼핑 사이트에 접속하지 않는다.
- 2026-11-14 (D64): 반응 이모지 — 요청 `client_features: ["reaction"]`, 응답 선택 키 `reaction`, features `reaction`. 그 요청에는 `none`의 `replies`가 빈 배열일 수 있다.
- 2026-11-14 (D63): 알림에 띄울 글 — features `push_text`, `GET /v1/tasks/{id}/notification?type=…`, 푸시의 `aps`에 `mutable-content: 1`. Apple을 거치는 내용은 그대로 고정 문장과 id다.
- 2026-11-13 (D44): 푸시 알림 — features `push`(맥에 키가 설정됐을 때만), `POST /v1/push/devices`, `DELETE /v1/push/devices/{token}`, 알림의 `type` 6개.
- 2026-11-13 (D62): 날씨 — 요청의 선택 키 `client_features`·`events`, 응답 `action.type: "weather"`, features `weather_action`. features `web_find_reads_cloud`(기본 없음). 사용자가 친 본인 주소는 Claude에 이름표로 간다(응답 꼴 변화 없음).
- 2026-11-13 (D61): 공개 정보 직접 찾기 — features `web_find`·`report_links`(·`web_find_naver`), `result.report.links [{title, url, site, image?}]`, `result.report.by`. 말풍선의 링크(`replies[].links`/`reply_links`)는 켠 적 없이 접었다.
- 2026-11-13 (D60, 빌드 36 뒤): 발송 승인의 메일 제목·본문은 마크다운 표시를 걷어 낸 일반 글이다(맥이 승인 전에 걷어 낸다. 꼴 변화 없음).
- 2026-11-12 (D58): 보고의 캡처를 켰다(features `report_attachments`). 붙는 때를 정했다 — 읽기 작업이 끝나면 읽은 화면, 로그인이 풀리거나 막혀서 멈추면 멈춘 화면, 취소한 작업에는 없음("언제 무엇이 붙나" 표). `sensitive: false`는 로그인이 없는 합성 사이트 화면뿐. 작업 `outcome`에 `site_blocked` 추가(status는 `failed`).
- 2026-11-12 (D56): 사이트 읽기 — features `site_read`, 작업 `outcome: "needs_login"` + `result.reason`(`login_expired`·`login_missing`) + `result.action`, 카드 `needs[].reason`, `GET /v1/sites`의 `reason`. 보고의 캡처 — features `report_attachments`(기본 끔), `result.report.attachments`, `GET /v1/tasks/{id}/attachments/{id}`, 404 `attachment_gone`. 모두 더해진 값이라 기존 앱은 그대로 동작한다.
- 2026-11-11 (r-004, D41): `approval.fields`·`approved_drafts.fields`가 원문(길이 제한 없음, `\n` 포함)으로 바뀜.
- 2026-11-11 (D42): `approval.draft_id`·`approval.sha256`·`approval.received` 추가, 승인 요청에 `sha256`(선택), 409 `hash_mismatch` 추가, `approved_drafts`에 `draft_id`·`sha256` 추가. 기존 필드는 그대로라 v1 앱도 동작한다.
- 2026-11-11 (D42 2단계): `POST /v1/messages`(대화 담당) 추가, 작업 객체에 `title`·`brief`(대화로 만든 작업일 때), 502 `model_error`.
- 2026-11-11 (D45, 3단계): health `api_version`·`features`, 상태 `awaiting_input` + `task.question` + `POST /v1/tasks/{id}/answer`, `task.narration`, `result.report`, 작업의 `title`(항상)·`conversation_id`, `GET /v1/tasks?conversation_id=`. 대화 담당은 지원하지 않는 서비스를 할 수 있다고 답하지 않고, 일을 약속하는 답은 작업이 만들어질 때만 보낸다(D46). `result.outcome`에 `verify_failed` 추가.
- 2026-11-12 (D55): 할 수 있는 일 카드 — features `skills`, `GET /v1/skills`. 대화의 "네이버 메일 연결해줘"는 `action: {"type":"site_login","site":"naver_mail"}`.
- 2026-11-12 (D54): 메일 읽기 — features `mail_read`, `GET /v1/mail/today`, `POST /v1/mail/briefing`, 대화의 "오늘 온 메일 요약". `GET /v1/sites`에 `naver_mail`(네이버 메일 연결 흐름).
- 2026-11-12 (D52): 사이트 로그인 — features `site_login`, `GET /v1/sites`, `POST /v1/sites/{site}/login`·`…/input`·`…/cancel`·`check`·`logout`, `X-Api-Version: 4`의 `action.type: "site_login"`.
- 2026-11-12 (D50, 4단계): 본인 주소 발송 — `X-Api-Version: 3`, 승인 `kind: "send"`, 상태 `send_pending` + `send`, `result.report.sent`, `outcome: "send_failed"` + `result.reason`, `approved_drafts[].kind`. 메일 계정 `GET /v1/mail/accounts`, `POST …/check`·`connect`·`disconnect`. health `features`에 `send_self`·`mail_connect`.
- 2026-11-12 (D49): 메일 부탁을 회의 답변 경로(`route_claude`)로 넘기던 것을 `ask` 되물음으로(실기기 r-009). `context` 머리 줄 약속(`그 날 녹음한 회의는 없습니다`). `X-Api-Version`은 2 이상이면 v2. 서버 기록에 판단 유형·안전장치·말투·걸린 시간(내용 없음).
- 2026-11-12 (D48): `POST /v1/messages`에 `tone`(friendly / formal), health `features`에 `tone`. 날씨·주가 같은 실시간 정보는 지어내지 않고 "확인할 수 없다"고 답한다.
- 2026-11-12: `POST /v1/messages`에 `client_message_id`(재시도 중복 방지), 응답에 `replayed`, health `features`에 `client_message_id`. 실기기에서 끊긴 요청 뒤 다시 보내 같은 작업 2개가 생기고 앞 작업이 승인 대기로 줄을 막은 일로 추가.
- 2026-11-11 (D45 호환 수정): `awaiting_input`은 `X-Api-Version: 2` 헤더로 만든 작업에서만 쓴다. 헤더가 없으면 v1처럼 `needs_input`으로 끝난다(배포된 앱이 모르는 상태를 실패로 처리하는 문제).

- 2026-11-15 (D73): 사진·PDF 붙여 묻기 — `POST /v1/uploads`(본문은 파일 그대로), `POST /v1/messages`의 선택 키 `uploads`, features `uploads`. 기존 키·형식은 그대로다.
- 2026-11-18 (D129): 오래 가는 기억 — `/v1/memory`(보기·고치기·되돌리기·찾기·모두 지우기), health `features`에 `memory`. 앱 화면은 앱 쪽 대기.
- 2026-11-19 (D132): 작업 중 끼어들기·함께 돌리기 — 대화 응답 `action: {"type":"steer","task_id","mode":"inject"}`(돌고 있는 작업에 덧붙임) 또는 `action: {"type":"task","task":{…},"steer":{"task_id":<거둔 작업>,"mode":"replace"}}`(승인 전 작업을 거두고 새로). 작업 상태에 `steers: [{time, text(200자), applied}]`, `status: "queued"`일 때 `wait_reason`(줄 선 까닭 한 줄). 작업은 자원이 다르면 함께 돈다(최대 3). health `features`에 `steer`.

## 알려 둘 제한
- 한 번에 한 작업만 실행한다. 로컬 모델이라 작업 하나에 수십 초~수 분이 걸린다.
- 에이전트의 판단 정확도는 높지 않다(`docs/EVALUATION.md`). 초안 내용은 반드시 사용자가 확인하고 승인하게 한다.
- 실사용에서 틀린 사례는 `docs/EVALUATION.md`의 "실사용 실패" 절에 시나리오로 추가한다.
