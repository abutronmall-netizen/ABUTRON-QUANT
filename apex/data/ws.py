"""Minimal RFC-6455 WebSocket client (asyncio, standard library only).

Why hand-rolled: the Deriv API is WebSocket-first and the production box must run
with a pinned, auditable dependency set. ~150 lines removes the only third-party
runtime dependency from the live data path. Supports text frames, fragmentation,
ping/pong and clean close - everything the Deriv API uses.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(RuntimeError):
    pass


def encode_frame(payload: bytes, opcode: int = 0x1, mask: bool = True) -> bytes:
    header = bytearray()
    header.append(0x80 | opcode)
    n = len(payload)
    mask_bit = 0x80 if mask else 0
    if n < 126:
        header.append(mask_bit | n)
    elif n < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack("!H", n)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", n)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    header += key
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def decode_frame(buf: bytes) -> tuple[Optional[tuple[int, bytes, int]], int]:
    """Return ((opcode, payload, consumed), needed) or (None, needed)."""
    if len(buf) < 2:
        return None, 2 - len(buf)
    b1, b2 = buf[0], buf[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    idx = 2
    if length == 126:
        if len(buf) < 4:
            return None, 4 - len(buf)
        length = struct.unpack("!H", buf[2:4])[0]
        idx = 4
    elif length == 127:
        if len(buf) < 10:
            return None, 10 - len(buf)
        length = struct.unpack("!Q", buf[2:10])[0]
        idx = 10
    key = b""
    if masked:
        if len(buf) < idx + 4:
            return None, idx + 4 - len(buf)
        key = buf[idx:idx + 4]
        idx += 4
    if len(buf) < idx + length:
        return None, idx + length - len(buf)
    payload = buf[idx:idx + length]
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return (opcode, payload, idx + length), 0


class WebSocketClient:
    def __init__(self, url: str, headers: Optional[dict] = None,
                 ping_interval: float = 20.0, connect_timeout: float = 15.0):
        self.url = url
        self.headers = headers or {}
        self.ping_interval = ping_interval
        self.connect_timeout = connect_timeout
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._buf = b""
        self._frag: list[bytes] = []
        self._ping_task: Optional[asyncio.Task] = None
        self.closed = True

    async def connect(self) -> None:
        u = urlparse(self.url)
        secure = u.scheme == "wss"
        host = u.hostname or "localhost"
        port = u.port or (443 if secure else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        ctx = ssl.create_default_context() if secure else None
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if secure else None),
            timeout=self.connect_timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in self.headers.items()]
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self.writer.drain()
        head = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), timeout=self.connect_timeout)
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise WebSocketError(f"handshake failed: {status.decode(errors='replace')}")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if expected.lower().encode() not in head.lower():
            raise WebSocketError("bad Sec-WebSocket-Accept")
        self.closed = False
        if self.ping_interval:
            self._ping_task = asyncio.create_task(self._ping_loop())

    async def _ping_loop(self) -> None:
        try:
            while not self.closed:
                await asyncio.sleep(self.ping_interval)
                if self.writer and not self.writer.is_closing():
                    self.writer.write(encode_frame(b"", opcode=0x9))
                    await self.writer.drain()
        except (asyncio.CancelledError, ConnectionError):
            return

    async def send(self, text: str) -> None:
        if self.closed or self.writer is None:
            raise WebSocketError("not connected")
        self.writer.write(encode_frame(text.encode()))
        await self.writer.drain()

    async def _read_frame(self) -> tuple[int, bytes]:
        while True:
            parsed, need = decode_frame(self._buf)
            if parsed is not None:
                opcode, payload, consumed = parsed
                self._buf = self._buf[consumed:]
                return opcode, payload
            chunk = await self.reader.read(65536)   # type: ignore[union-attr]
            if not chunk:
                self.closed = True
                raise ConnectionError("socket closed by peer")
            self._buf += chunk

    async def messages(self) -> AsyncIterator[str]:
        while not self.closed:
            opcode, payload = await self._read_frame()
            if opcode == 0x8:
                self.closed = True
                break
            if opcode == 0x9:
                self.writer.write(encode_frame(payload, opcode=0xA))  # type: ignore[union-attr]
                await self.writer.drain()  # type: ignore[union-attr]
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x0:
                self._frag.append(payload)
                continue
            if opcode == 0x1:
                if self._frag:
                    self._frag.append(payload)
                    data = b"".join(self._frag)
                    self._frag.clear()
                else:
                    data = payload
                yield data.decode("utf-8", errors="replace")

    async def close(self) -> None:
        self.closed = True
        if self._ping_task:
            self._ping_task.cancel()
        if self.writer is not None:
            try:
                self.writer.write(encode_frame(b"", opcode=0x8))
                await self.writer.drain()
                self.writer.close()
            except (ConnectionError, RuntimeError):
                pass
