# 푸시 알림 설계 (D44, 설계만 — 구현 전)

배경: 앱이 백그라운드로 가면 상태 조회가 멈춰 승인 요청을 놓친다. 앱은 스스로 알림을 띄울 수 없으니 맥 서버가 APNs로 보낸다.
출발점은 회의 앱 세션의 제안(기기 등록 API, 고정 문구 + task id, health features)이다. 아래는 그 제안을 검토해 정한 형태다.
`api.push.apple.com` 접속은 사용자 승인(2026-11-11).

## 0. 2026-11-13 앱 세션과 다시 맞춘 형식 (아래 1~3절과 다른 곳은 이 절이 우선한다. 구현 전)
- 등록: `POST /v1/push/devices {"token": "<16진수 64~200자>", "environment": "production"|"sandbox", "bundle_id"?: "…"}` → `200 {"registered": true}`(같은 토큰은 덮어쓰기),
  `DELETE /v1/push/devices/{token}` → `200 {"registered": false}`(없어도 200). 기기 10개를 넘으면 가장 오래 쓰지 않은 등록을 지운다(오류 아님).
  `400 invalid_request`: 토큰 꼴, environment 값, **sandbox(그 호스트 접속은 승인되지 않았다 — 받아 두고 안 보내지 않는다)**, `bundle_id`가 맥 설정(`APNS_TOPIC`)과 다름. 푸시가 설정돼 있지 않으면 이 경로는 `404 not_found`.
- 알림: `{"aps": {"alert": {"body": "<고정 문장>"}, "sound": "default", "thread-id": "<task_id>"}, "type": "…", "task_id": "…", "conversation_id": "…"}` — title·배지 없음.
  내용(작업 제목·초안·받는 사람·사이트 이름·읽은 결과)은 넣지 않는다. 대화 답(action none)에는 보내지 않는다.

| type | 언제 | 고정 문장 | 한 번만(중복 판단) |
|---|---|---|---|
| `approval_needed` | 승인 대기 | 승인이 필요한 일이 있어요 | task_id + approval_id |
| `input_needed` | 작업 담당의 질문 | 확인이 필요한 질문이 있어요 | task_id + 질문 id |
| `task_done` | 완료 | 맡긴 일이 끝났어요 | task_id |
| `task_failed` | 실패 | 맡긴 일을 끝내지 못했어요 | task_id |
| `needs_login` | 로그인이 풀려 멈춤 | 로그인이 필요해서 멈췄어요 | task_id |
| `routine_result` | (루틴이 생기면) | 루틴 결과가 도착했어요 | task_id |

- 사용자가 취소한 작업, 10초짜리 발송 대기는 보내지 않는다. 승인 대기 시간(30분)은 늘리지 않는다 — 푸시를 넣고 재 본 뒤 정한다.
- 앱: features에 `push`가 있고 알림을 허용했을 때만 등록, 모르는 type은 무시, 누르면 그 대화의 작업 카드로 간다(내용은 기존 `GET /v1/tasks/{id}`).
- 루틴(예약 실행)은 D80으로 만들었다(`docs/API.md` "예약해 둔 일"). 아래는 그 전의 메모: 루틴(예약 실행)은 설계가 없다. 형식 초안만 앱 세션과 주고받았다(정해진 일 하나 + 시간표, 모델이 혼자 만들지 않고 사용자가 눌러야 생김, 읽기만).
  프로젝트 지침(저장소 밖)의 "능동 실행은 하지 않는다"를 사용자가 바꾸고, 결과 보관(메모리 / 암호화)·맥이 잠든 때·로그인이 풀린 때를 정한 뒤에 설계한다.

## 1. 앱 쪽 API
| 메서드·경로 | 요청 | 응답 |
|---|---|---|
| `POST /v1/devices` | `{"apns_token": "<16진수 64~200자>", "environment": "production" \| "sandbox"}` | `200 {"registered": true}`. 같은 토큰이면 덮어쓰기(environment 갱신) |
| `DELETE /v1/devices/{apns_token}` | — | `200 {"registered": false}` (없어도 200) |

- 인증은 다른 경로와 같은 `X-App-Token`. 기기는 최대 10개(넘으면 가장 오래 쓰지 않은 것부터 지움).
- `GET /v1/health`의 `features`에 `"push"`는 **서버에 APNs 키가 설정되어 있을 때만** 넣는다. 앱은 그때만 토큰을 등록한다(health의 `api_version`·`features`는 3단계에서 함께 넣는다).
- 오류: 400 `invalid_request`(토큰 형식·environment).

## 2. 언제 보내나
작업 상태가 바뀔 때, 같은 일로는 한 번만.
| 사건 | 서버 상태 | 알림 본문(고정) | 중복 판단 키 |
|---|---|---|---|
| 승인 요청 | `awaiting_approval` | "승인이 필요해요" | task_id + approval_id (사유를 달아 거부한 뒤 새 초안이 오면 새 알림) |
| 완료 | `completed` | "작업이 끝났어요" | task_id + 상태 |
| 질문 | `needs_input` | "확인이 필요해요" | task_id + 상태 |
| 실패 | `failed` | "작업을 끝내지 못했어요" | task_id + 상태 |
| (4단계) 발송 대기 | `send_pending` | 보내지 않는다. 10초짜리라 알림이 오히려 혼란을 준다 | — |

- 제안서의 `needs_approval`은 서버 상태 이름 `awaiting_approval`로 맞춘다. `cancelled`는 사용자가 한 일이라 보내지 않는다.
- 앱이 화면에 떠 있어도 서버는 구분하지 않고 보낸다. 앞에 떠 있을 때 알림을 숨길지는 앱이 정한다(`willPresent`).

## 3. 알림 내용
```json
{"aps": {"alert": {"title": "맥 에이전트", "body": "승인이 필요해요"}, "sound": "default",
         "thread-id": "<task_id>"},
 "task_id": "<task_id>", "event": "awaiting_approval"}
```
- **고정 문구와 task_id·event만 보낸다.** 작업 제목·초안·받는 사람·회의 내용은 넣지 않는다(D7). 작업 제목은 모델이 사용자 문장으로 만든 것이라 이름이 들어갈 수 있어 제외했다.
- 헤더: `apns-push-type: alert`, `apns-topic: <앱 번들 ID>`, `apns-collapse-id: <task_id>`(같은 작업의 알림은 최신 것으로 교체),
  `apns-priority: 10`, `apns-expiration`: 승인 요청은 승인 만료 시각(30분), 나머지는 24시간.

## 4. 인증 정보 (저장소·로그·API 응답에 절대 넣지 않는다)
- APNs 인증 키(.p8, 토큰 방식) 내용은 **맥 키체인**에 둔다: 서비스 이름 `local-ai-agent.apns`. 사용자가 직접 넣는다(아래 명령). 서버는 `security find-generic-password -w`로 읽어 메모리에만 둔다.
- Key ID·Team ID·번들 ID(topic)는 비밀은 아니지만 저장소 밖 설정에만: `.env`(git 제외)의 `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_TOPIC`.
- 같은 방식(키체인)을 4단계 메일 계정 인증 정보에도 쓴다.
- 등록한 기기 토큰은 `~/.local/share/local-ai-agent/devices.json`(권한 600)에 저장해 서버를 재시작해도 남긴다. 로그에는 앞 6자만.

```sh
# 사용자가 직접 실행 (키 파일 내용이 대화·로그에 남지 않게)
security add-generic-password -U -s local-ai-agent.apns -a apns-p8 -w "$(cat ~/Downloads/AuthKey_XXXXXXXXXX.p8)"
```

## 5. 보내는 방법
- APNs는 HTTP/2만 받는다 → `httpx[http2]`(패키지 `h2` 추가). JWT(ES256) 서명 → `cryptography` 추가. 둘 다 작은 순수 파이썬/휠 패키지.
- JWT는 50분마다 새로 만든다(APNs 규칙: 1시간 이내, 20분보다 자주 바꾸지 말 것).
- 주소: production → `api.push.apple.com`, sandbox → `api.sandbox.push.apple.com`(Xcode로 직접 설치한 개발 빌드용). TestFlight·App Store 빌드는 production.
- **작업 진행을 막지 않는다**: 상태 변경 때 대기열에 넣고 별도 스레드가 보낸다. 요청 시간 제한 10초.
- 응답 처리: 200 성공 / 410 `Unregistered`·400 `BadDeviceToken` → 그 기기 등록 삭제 / 403 `ExpiredProviderToken` → JWT 새로 만들고 1번 재시도 / 429·5xx → 2초 뒤 1번 재시도 후 포기. 로그에는 상태 코드·이유·토큰 앞 6자만.
- 연결 방향은 맥 → Apple 바깥쪽뿐. 들어오는 포트는 늘지 않는다.

## 6. 코드 위치(예정)
- `src/agent/api/push.py`: `DeviceStore`(파일 저장), `ApnsClient`(JWT·HTTP/2·응답 처리, 전송 계층 교체 가능), `Notifier`(상태 변경 → 알림, 중복 제거, 대기열).
- `JobManager`에 상태 변경 콜백(`on_status(job, old, new)`) 하나 추가. 승인 요청 시점에는 approval_id를 넘긴다.
- `server.py`: `/v1/devices` 경로, health `features`.

## 7. 확인 방법
- 단위: 가짜 전송 계층으로 사건별 알림 1번·중복 없음·내용에 고정 문구만 있음(초안·제목 문자열이 페이로드에 없는지 검사), 410이면 등록 삭제, 403이면 JWT 갱신 후 재시도, 전송 실패가 작업을 막지 않음. JWT는 테스트용으로 만든 키로 서명해 공개 키로 검증.
- 실제: 사용자 기기 1대에 승인 요청·완료 알림이 오는지, 상태 변경부터 도착까지 시간. 알림을 눌러 앱이 해당 작업을 여는지는 앱 쪽 확인.

## 8. 남은 결정
- 구현 시점(3단계와 함께 / 3단계 뒤)
- 패키지 2개 설치(`h2`, `cryptography`)
- `api.sandbox.push.apple.com` 접속(개발 빌드용, 승인된 주소와 다른 호스트)
- 키체인에 .p8 넣기는 사용자가 직접
