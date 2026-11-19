# 본인 주소로 실제 메일 발송 설계 (D42 4단계 — 구현됨 D50, 합성 메일 서버로 검증. 실제 계정 확인 전)

D42에서 정한 범위: **사용자 본인 주소로만** 실제 메일을 보낸다. 브라우저가 아니라 메일 전용 도구로 보내고,
승인한 초안의 해시와 같을 때만, 승인 후 10초 동안 취소할 수 있게 한 뒤 보낸다. 보낸 뒤에는 실제로 도착했는지 확인한다.

## 1. 흐름
```
대화 담당: "나한테 회의록 보내 줘" → task (targets: 본인 주소 — 허용 목록에서)
작업 담당: 내용 준비(읽기·정리) → compose_mail(to, subject, body) 도구 호출
서버:      ① 정책 검사(받는 사람 ⊆ 허용 목록, 아니면 policy_denied)
           ② 초안 고정: .eml 생성(Message-ID·Date를 이때 정함) → fields 해시(sha256) + eml 해시(eml_sha256)
           ③ 승인 요청 kind "send" → 앱 승인(sha256 포함)
           ④ 상태 send_pending 10초 (이때 cancel 가능)
           ⑤ 보내기 직전 .eml을 다시 읽어 eml_sha256 확인 → SMTP 발송(조건: --expect-sha256과 같을 때만)
           ⑥ 검증: SMTP 접수 응답 → IMAP으로 받은편지함(본인 주소라 도착함)·보낸편지함에서 Message-ID로 찾아 본문 대조
           ⑦ report.sent에 message_id, 발송 시각, 검증 결과
```
- 모델은 메일을 '보내지' 않는다. `compose_mail`은 초안을 만들 뿐이고, 보내는 것은 승인·대기·해시 확인을 모두 통과한 뒤 서버 코드다.
- 브라우저의 '보내기' 버튼은 지금처럼 정책에서 금지한 채로 둔다(read_draft). 발송 경로는 메일 도구 하나뿐.

## 2. 허용 목록과 "나"
- 허용 목록: `.env`(git 제외)의 `SEND_SELF_ADDRESSES`(쉼표로 구분, 본인 주소만). 저장소·문서·로그에 실제 주소를 쓰지 않는다.
- 받는 사람·참조·숨은 참조 **모두** 허용 목록 안이어야 한다. 하나라도 밖이면 정책 거부(`policy_denied`, 승인 대상도 아님).
- 대화 담당은 허용 목록 첫 주소를 "나(사용자 본인)"로 안다 → 실기기에서 본 "'나한테'인데 주소를 다시 묻는" 되물음이 없어진다.
  이 주소는 맥 안의 로컬 모델에만 들어간다.
- 발송 횟수 제한: 시간당 5통, 하루 20통(계정 정지·스팸 판정 방지). 넘으면 `blocked`에 이유를 쓴다.

## 3. 계정과 인증 정보
- 1순위: **SMTP(발송) + IMAP(검증) + 앱 비밀번호.** 표준 라이브러리(`smtplib`, `imaplib`, `email`)만으로 되고 추가 패키지가 없다.
  - Gmail: 2단계 인증 + 앱 비밀번호, `smtp.gmail.com:465`(SSL), `imap.gmail.com:993`
  - 네이버: 환경설정에서 POP3/IMAP·SMTP 사용 + 앱 비밀번호, `smtp.naver.com:465`, `imap.naver.com:993`
- 2순위(나중): Gmail API(OAuth). 토큰 갱신·동의 화면이 필요해 복잡하다. 앱 비밀번호를 쓸 수 없는 계정일 때만.
- 인증 정보는 **맥 키체인**(서비스 `local-ai-agent.mail`, 계정 = 메일 주소)에만. 사용자가 직접 넣는다. 서버는 쓸 때만 읽고 메모리에 오래 두지 않는다.
  저장소·로그·API 응답에 절대 넣지 않고, 앱은 보지 않는다(푸시 키와 같은 방식, D44).
  ```sh
  security add-generic-password -U -s local-ai-agent.mail -a <본인 주소> -w   # 입력 창에 앱 비밀번호
  ```
- 서비스별 사용법은 문서(skill)로 둔다: `docs/skills/mail_gmail.md`, `docs/skills/mail_naver.md` — 앱 비밀번호 만드는 법,
  호스트·포트, 보낸편지함 폴더 이름(Gmail `[Gmail]/Sent Mail`, 네이버 `Sent Messages`), 알려진 제한. 작업 담당 프롬프트에는 넣지 않는다(서버 코드가 쓴다).

## 4. 고정과 해시
- `fields` = {"받는 사람", "제목", "본문"} 원문. 앱이 이미 쓰는 계산식 `draft_sha256(kind="send", action="보내기", url="mailto:<받는 사람>", fields)`를 그대로 쓴다 → 앱 코드 변경 없이 검증 가능.
- `.eml`은 fields와 고정한 Message-ID·Date로 결정적으로 만든다(UTF-8, quoted-printable). `eml_sha256`은 파일 바이트의 해시.
- 발송 직전 확인 두 가지: 승인한 sha256 == 고정 초안의 sha256, 디스크의 .eml 해시 == eml_sha256. 하나라도 다르면 보내지 않고 `approval_mismatch`.
- .eml은 `~/.local/share/local-ai-agent/outbox/`(권한 700)에 두고, 발송·검증이 끝나면 지운다(본문이 실제 개인 정보일 수 있다).

## 5. API 변경 (v2, `features`에 `"send_self"`가 있을 때만 앱이 켠다)
- 승인: `approval.kind = "send"`, `action = "보내기"`, `url = "mailto:<받는 사람>"`, `fields`, `sha256`, `draft_id`. 승인 POST는 지금과 같다.
- 새 상태 `send_pending`: `send.send_at`(보낼 시각), `send.cancel_until`. 이때 `POST /v1/tasks/{id}/cancel` → 보내지 않고 `cancelled`.
- 결과: `result.report.sent = [{"draft_id", "sha256", "to", "message_id", "sent_at", "verified": "delivered" | "accepted_only" | "failed", "detail"}]`.
  `verified`가 `delivered`가 아니면 앱은 "보냈지만 도착 확인 못 함"을 보여 준다.
- 오류: 발송 실패(인증 실패·연결 실패)는 작업 실패 `outcome: "send_failed"`, 상세 사유는 종류만(비밀번호·서버 응답 원문은 넣지 않음).

## 6. 확인 방법
- **합성 메일 서버 먼저**: 테스트 망(laa-testnet) 안에 SMTP 받기 + IMAP 조회가 되는 테스트 메일 서버를 둔다.
  후보: (a) 테스트 사이트에 최소 SMTP 수신기 + 조회용 HTTP를 직접 구현(표준 라이브러리, 설치 없음) (b) `aiosmtpd` 패키지 (c) GreenMail 같은 이미지(다운로드).
  → (a)를 추천: 이미 testsite가 있고 외부 의존이 없다. IMAP 검증 코드는 실제 계정용이라 (a)에서는 조회용 HTTP로 대신 검증하고, IMAP 경로는 단위 테스트(가짜 IMAP)로 확인.
- 평가 시나리오(가상 데이터): 본인 주소로 회의록 보내기 / 남의 주소로 보내 달라는 요청(정책 거부·0통) / 승인 후 10초 안에 취소(0통) /
  승인 뒤 초안이 바뀐 경우(해시 불일치·0통) / 허용 목록 밖 참조 / 발송 횟수 제한. 지표: 승인 없는 발송 0, 허용 목록 밖 발송 0, 승인 내용과 발송 내용 일치(바이트 해시).
- **실제 계정은 본인 주소 1회 확인만**: 받은편지함 도착·본문 일치·report 값.

## 7. 구현 전에 정할 것 (사용자)
1. 쓸 메일 계정(Gmail / 네이버 / 기타)과 앱 비밀번호 발급(사용자가 직접), 키체인 등록
2. 외부 접속 승인: 고른 서비스의 SMTP·IMAP 호스트(예: `smtp.gmail.com`, `imap.gmail.com`)
3. 합성 메일 서버 방식((a) 추천)
4. 발송 횟수 제한 값

**사용자 결정 (2026-11-12, 이 세션에서 직접)**: 2번 Gmail·네이버 SMTP·IMAP 호스트 접속 **승인** / 3번 합성 메일 서버는 **(a)** /
4번 횟수 제한은 **시간당 5통·하루 20통**. 1번(계정 둘 다, 기본 네이버)은 회의 앱 세션 전달 내용 그대로 진행. 7-2(앱에서 계정 연결)도 보안 설명 뒤 **승인**("진행"). 구현과 실제 형식은 `docs/API.md` "본인 주소 발송"이 기준이다(이 문서의 5절과 다른 곳: `verified`에 `failed` 없음, `result.reason`, `send.fields`·`now`).

## 7-1. 앱 세션과 맞춘 형식 (2026-11-12, 제안 — 구현 전)
회의 앱 세션이 전한 사용자 말: 쓸 계정은 **Gmail·네이버 둘 다**(7절 1번). 2~4번은 아직 결정 전이다.
- **계정이 둘일 때 "나"**: 허용 목록 첫 주소가 기본 계정. "네이버로 보내 줘"처럼 서비스 이름을 말하면 그 계정. 첫 구현은
  보내는 계정 = 받는 주소(같은 계정의 IMAP 하나로 보낸편지함·받은편지함을 확인). 계정 사이 발송은 범위 밖.
- **승인 카드**: `fields = {"보내는 사람", "받는 사람", "제목", "본문"}` — 보내는 계정도 해시에 들어간다. 계산식은 그대로(키 정렬).
- **`GET /v1/mail/accounts`**: `{"accounts": [{"provider": "gmail"|"naver", "address", "status": "ready"|"auth_failed"|"unreachable"|"not_set",
  "default", "checked_at"}]}`. 허용 목록의 주소를 전부 돌려주고 비밀번호가 없으면 `not_set`(목록이 비면 빈 배열,
  `provider`는 주소의 도메인으로 정한다). 주소는 가리지 않는다(승인 카드에 원문이 간다). 비밀번호·서버 응답 원문은 없다. `status`는 마지막 확인 결과이고
  `POST /v1/mail/accounts/check`로 다시 확인한다.
- **이전 빌드 호환**: `X-Api-Version: 3`을 보낸 요청에서만 발송 작업(kind `send`, `send_pending`)을 만든다. 2 이하면 지금처럼 초안까지.
  3은 2(awaiting_input)를 포함한다. 서버는 "2 이상이면 v2"로 본다(예전에는 정확히 "2"만 인정했다).
- **`send_pending`**: `send.send_at`·`send.cancel_until`·`send.now`(서버 현재 시각) 모두 UTC ISO 8601. 앱은 `cancel_until − now`로 남은 초를 센다.
  늦은 취소(이미 보냄)는 409 `conflict`, 앱은 다시 조회해 `report.sent`를 보여 준다.
- **승인 전 로그인 확인**: 초안을 고정할 때 SMTP·IMAP 로그인만 해 본다(보내지 않음). 실패하면 승인을 요청하지 않고
  `send_failed`(사유 종류: `auth_failed` | `unreachable` | `not_set`).
- **연결**: SMTP는 465(SSL) 먼저, 실패하면 587(STARTTLS). 폴더는 이름을 고정하지 않고 LIST의 SPECIAL-USE(`\Sent`, `\Drafts`)로 찾고
  없을 때만 알려진 이름. 네이버는 웹 메일 환경설정에서 IMAP/SMTP "사용함" + 2단계 인증·앱 비밀번호가 필요하다는 안내가 있다(구현 때 실제 계정으로 재확인).

- **실패 사유**: `outcome: "send_failed"`일 때 `result.reason`(문자열 하나): 승인 전 `auth_failed` | `unreachable` | `not_set` |
  `rate_limited`(횟수 제한) | `not_allowed`(받는 사람이 허용 목록 밖 — 보통은 대화 담당이 먼저 거절해 작업이 안 생긴다),
  승인 뒤 `rejected`(SMTP 거절) | `unreachable`. 허용 목록이 비어 있을 때 "나한테 보내 줘"는 `not_set`. 어느 경우든 보낸 메일은 0통이고 `report.blocked`에 사람이 읽는 이유 한 줄. 앱은 모르는 값을 일반 실패로 보여 준다.
- **`send_pending` 응답**: `approval`은 없고 `send = {"draft_id", "sha256", "from", "to", "fields", "send_at", "cancel_until", "now"}`.
  `fields`는 승인한 것과 같은 원문(앱이 들고 있는 내용 대신 써도 된다).
- **보낸 뒤**: `result.approved_drafts`에 `{"draft_id", "sha256", "kind": "send", "action": "보내기", "url": "mailto:…", "fields"}`
  (`kind`는 저장 초안에도 `"draft"`로 넣는다 — 추가 필드). `report.sent[]`와는 `sha256`으로 짝을 맞춘다.
- **`report.sent`에는 SMTP가 접수한 메일만** 넣는다. `verified`는 `delivered` | `accepted_only` 둘뿐(5절의 `failed`는 뺀다):
  SMTP가 거절하면 `sent`는 비고 작업은 `send_failed`다. 그래서 `completed`인데 보내지 못한 경우는 없다.
- **`POST /v1/mail/accounts/check`**: 사용자가 "다시 확인"을 누를 때만. 계정마다 로그인해 보므로 최대 20초쯤 걸릴 수 있다.

## 7-2. 앱에서 메일 계정 연결 `mail_connect` (2026-11-12, 사용자 승인 후 구현)
회의 앱 세션이 전한 사용자 방향: 계정 문제로 못 보내면 채팅에 카드가 뜨고, 앱의 시트에서 앱 비밀번호를 넣는다. 맥에 묶이지 않게
(나중에 서버가 받을 수도 있음). 기본 계정은 네이버. **이것은 3절의 "인증 정보는 사용자가 맥에서 직접 넣고 앱은 보지 않는다"(D42)를 바꾸는 일이라
사용자가 이 저장소 세션에 직접 승인해야 한다.**
- `POST /v1/mail/accounts/connect` `{"provider": "naver"|"gmail", "address", "app_password"}` → `{"accounts": [...]}`(7-1과 같은 형식).
  `health.features`에 `"mail_connect"`가 있을 때만 앱이 보낸다. 로그인 확인까지 한 번에(최대 20초쯤).
- 보안 요구: 본문을 로그·작업 기록에 남기지 않음 / 모델 컨텍스트에 넣지 않음 / 보안 저장소에만 보관 / 로그인 실패면 보관하지 않음 /
  쓰기 전용(어떤 응답으로도 돌려주지 않음) / 오류에 메일 서버 원문·비밀번호·요청 본문을 넣지 않음 / 보관 뒤 메모리의 값은 바로 버림.
- 허용 목록과의 관계: 보낼 수 있는 주소는 실행하는 쪽 설정(`SEND_SELF_ADDRESSES`)에서만 정한다. connect는 그 목록에 있는 주소의 비밀번호만 받고,
  없는 주소는 403 `not_allowed`(보관·로그인 시도 없음) → 앱 토큰만으로 보낼 수 있는 주소가 늘어나지 않는다.
- 횟수 제한: 주소당 10분에 5번, 넘으면 429 `rate_limited`. 해제: `POST /v1/mail/accounts/disconnect` `{"address"}` → 보관한 값 삭제.
- 응답·오류·API 문서에 맥 전용 이름(키체인 서비스 이름, 경로)을 드러내지 않는다 — "실행하는 쪽의 보안 저장소"라고만 쓴다.

## 8. (설계만) 담당자 개별 발송 — 5단계 미리 보기
- 여러 통을 한 번에 승인: `approval = {kind: "send_batch", count, representative: {대표 1통의 fields}, items: [{to, name, sha256}], sha256: <items 해시들의 해시>}`.
- 주소는 앱이 보낸 contacts에 있는 것만. 없으면 ask. 각 메일에는 받는 사람의 이름과 그 사람의 할 일만.
- 팀 공지만 한 통(받는 사람 여러 명). 이 단계는 본인 주소 제한(D42 1단계)을 풀어야 하므로 따로 승인받는다.
