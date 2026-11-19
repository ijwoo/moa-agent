"""작업실의 나가는 문(D96): 작업실 컨테이너의 유일한 바깥 길.

- HTTP CONNECT(https)와 평범한 HTTP 요청만 받는다. 포트는 80·443만.
- 이름을 여기서 풀고, **공인 IP(is_global)일 때만** 그 IP로 직접 잇는다 — 맥 서버·사설망·tailnet(100.64/10)·루프백·링크로컬은 막는다.
  (풀린 IP로 직접 잇기 때문에 DNS 재바인딩으로 우회되지 않는다)
- 기록에는 시각·호스트·포트·결과만 남긴다(내용·경로는 남기지 않는다).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import sys
import time

PORTS = {80, 443}
LISTEN = ("0.0.0.0", 3128)
IDLE_S = 120


def log(host: str, port: int, result: str) -> None:
    sys.stdout.write(f"[egress] {time.strftime('%H:%M:%S')} {host}:{port} {result}\n")
    sys.stdout.flush()


async def resolve_public(host: str, port: int) -> str | None:
    """공인 IP 하나(없으면 None). 하나라도 공인이 아니면 그 주소는 쓰지 않는다."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    for *_, addr in infos:
        ip = ipaddress.ip_address(addr[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_global:
            return str(ip)
    return None


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(r.read(65536), IDLE_S)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 15)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        cw.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, version = lines[0].split(" ", 2)
    except ValueError:
        cw.close()
        return
    if method == "CONNECT":
        host, _, p = target.rpartition(":")
        port, rest = int(p or 443), b""
    else:   # 평범한 HTTP: 절대 주소만
        if not target.startswith("http://"):
            cw.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            cw.close()
            return
        hostport = target[7:].split("/", 1)[0]
        host, _, p = hostport.partition(":")
        port = int(p or 80)
        path = "/" + target[7:].split("/", 1)[1] if "/" in target[7:] else "/"
        rest = f"{method} {path} {version}\r\n".encode("latin-1") + "\r\n".join(
            l for l in lines[1:] if l and not l.lower().startswith("proxy-")).encode("latin-1") + b"\r\n\r\n"
    host = host.strip("[]")
    if port not in PORTS:
        log(host, port, "denied_port")
        cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        cw.close()
        return
    ip = await resolve_public(host, port)
    if ip is None:
        log(host, port, "denied_private")
        cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        cw.close()
        return
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, port), 15)
    except (asyncio.TimeoutError, OSError):
        log(host, port, "unreachable")
        cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        cw.close()
        return
    log(host, port, "ok")
    if method == "CONNECT":
        cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await cw.drain()
    else:
        uw.write(rest)
        await uw.drain()
    await asyncio.gather(pipe(cr, uw), pipe(ur, cw))


async def main() -> None:
    server = await asyncio.start_server(handle, *LISTEN)
    log("listen", LISTEN[1], "ready")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
