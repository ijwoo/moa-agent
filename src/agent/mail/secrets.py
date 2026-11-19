"""메일 계정 인증 정보 보관. 쓰기·읽기·삭제만 있고, 값은 로그·예외 메시지·API 응답에 넣지 않는다.

기본은 맥 키체인(`security` 명령). 저장할 때 비밀번호를 명령줄 인자로 넘기지 않는다(프로세스 목록에 보임) —
`security -i`의 표준 입력으로 넘긴다. 테스트는 MemorySecrets를 쓴다.
"""

from __future__ import annotations

import re
import subprocess
from typing import Protocol

SERVICE = "local-ai-agent.mail"
# 앱 비밀번호 형식: 공백 없는 ASCII. 따옴표·역슬래시는 security -i의 인자 해석을 깨므로 받지 않는다
PASSWORD = re.compile(r"^[A-Za-z0-9!#$%&()*+,\-./:;<=>?@\[\]^_{|}~]{8,128}$")
_ADDR = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class SecretStore(Protocol):
    def get(self, address: str) -> str | None: ...
    def set(self, address: str, password: str) -> None: ...
    def delete(self, address: str) -> None: ...


class MemorySecrets:
    def __init__(self, initial: dict[str, str] | None = None):
        self._d = dict(initial or {})

    def get(self, address: str) -> str | None:
        return self._d.get(address)

    def set(self, address: str, password: str) -> None:
        self._d[address] = password

    def delete(self, address: str) -> None:
        self._d.pop(address, None)


class KeychainSecrets:
    """맥 로그인 키체인. 서비스 이름은 고정, 계정 = 메일 주소."""

    def __init__(self, service: str = SERVICE):
        self.service = service

    def get(self, address: str) -> str | None:
        if not _ADDR.match(address):
            return None
        r = subprocess.run(["security", "find-generic-password", "-s", self.service, "-a", address, "-w"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.rstrip("\n") if r.returncode == 0 and r.stdout.strip() else None

    def set(self, address: str, password: str) -> None:
        if not _ADDR.match(address) or not PASSWORD.match(password):
            raise ValueError("주소 또는 비밀번호 형식 오류")
        cmd = f'add-generic-password -U -s {self.service} -a "{address}" -w "{password}"\n'
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or self.get(address) != password:
            raise RuntimeError("보안 저장소에 저장하지 못했다")   # 출력에는 값이 섞일 수 있어 넣지 않는다

    def delete(self, address: str) -> None:
        if _ADDR.match(address):
            subprocess.run(["security", "delete-generic-password", "-s", self.service, "-a", address],
                           capture_output=True, text=True, timeout=10)
