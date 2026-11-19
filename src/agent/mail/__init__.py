"""본인 주소로 실제 메일 발송 (D42 4단계, 설계 docs/design/send_self.md).

모델은 메일을 보내지 않는다. 모델은 compose_mail 도구로 초안을 낼 뿐이고, 정책 검사 → 초안 고정(.eml·해시) →
승인 → 대기(취소 가능) → 해시 재확인 → SMTP 발송 → 도착 확인은 모두 서버 코드가 한다.
"""

from .service import Draft, MailError, Mailer, Mismatch  # noqa: F401
from . import briefing  # noqa: E402,F401
