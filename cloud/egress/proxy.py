"""Изходящият път на задачите: HTTPS прокси, което пуска САМО към API на модели.

Контейнерите със задачи са в Docker мрежа без изход (`--internal`). Единственият
им път навън е този процес (`HTTPS_PROXY=http://genesis-egress:3128`), а той
приема само `CONNECT <разрешен-хост>:443`. Всичко останало — обикновен HTTP,
друг порт, IP адрес вместо име, хост извън списъка — получава 403 и се логва.

Така код на клиент не може да тегли произволни неща, да праща данни навън или
да атакува други машини. Моделът пак е достъпен, защото агентът без него не
работи.

    python -m cloud.egress.proxy            # слуша на 0.0.0.0:3128
    EGRESS_ALLOW="api.groq.com,ollama.com" python -m cloud.egress.proxy
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os

# Точно хостовете от веригата на моделите (genesis_agent/brain.py). Поддомейн
# на разрешен хост също минава: "x.api.groq.com" — да; "api.groq.com.evil" — не.
DEFAULT_ALLOW = (
    "api.groq.com", "integrate.api.nvidia.com", "ollama.com", "openrouter.ai",
    "generativelanguage.googleapis.com", "api.cerebras.ai", "api.sambanova.ai",
    "api.together.xyz", "router.huggingface.co", "api.deepseek.com",
    "api.openai.com", "api.cohere.ai", "api.anthropic.com",
)
PORT = 443
HEADER_LIMIT = 8192
IDLE_TIMEOUT = 300.0

log = logging.getLogger("genesis.egress")


def allowed_hosts() -> tuple[str, ...]:
    raw = os.environ.get("EGRESS_ALLOW", "")
    hosts = tuple(h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip())
    return hosts or DEFAULT_ALLOW


def is_allowed(target: str, allow: tuple[str, ...]) -> tuple[bool, str]:
    """`target` е "хост:порт" от CONNECT реда. Връща (разрешено ли, причина)."""
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit():
        return False, "без порт"
    if int(port) != PORT:
        return False, f"порт {port}"
    host = host.strip("[]").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return False, "IP адрес"
    except ValueError:
        pass
    if not host or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in host):
        return False, "невалиден хост"
    if any(host == a or host.endswith("." + a) for a in allow):
        return True, "ok"
    return False, "извън списъка"


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), IDLE_TIMEOUT)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass  # затваряне на вече счупена връзка


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 allow: tuple[str, ...]) -> None:
    peer = writer.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    if len(parts) != 3 or parts[0] != "CONNECT":
        log.warning("отказ %s: %r (само CONNECT)", peer, line[:120])
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    ok, why = is_allowed(parts[1], allow)
    if not ok:
        log.warning("отказ %s: %s (%s)", peer, parts[1], why)
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    host, _, port = parts[1].rpartition(":")
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(host.strip("[]"), int(port)), 15)
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("няма връзка към %s: %s", parts[1], e)
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    log.info("пуснат %s → %s", peer, parts[1])
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


# 0.0.0.0: слуша само във вътрешните Docker мрежи (виж cloud/up.sh).
async def serve(host: str = "0.0.0.0", port: int = 3128) -> None:
    allow = allowed_hosts()
    server = await asyncio.start_server(lambda r, w: handle(r, w, allow), host, port,
                                        limit=HEADER_LIMIT)
    log.info("egress на %s:%d, разрешени: %s", host, port, ", ".join(allow))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(serve(port=int(os.environ.get("EGRESS_PORT", "3128"))))
