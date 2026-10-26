"""Proxy TCP async que congela el tráfico tras el login de Postgres."""

from __future__ import annotations

import asyncio
import contextlib
import struct


def _buffer_has_ready_for_query(buf: bytes) -> bool:
    """True si el buffer contiene un mensaje backend ReadyForQuery (Z)."""

    i = 0
    if buf[:1] in {b"N", b"S"}:
        i = 1
    while i + 5 <= len(buf):
        msg_type = buf[i]
        length = struct.unpack_from("!I", buf, i + 1)[0]
        if length < 4:
            return False
        end = i + 1 + length
        if end > len(buf):
            return False
        if msg_type == ord("Z"):
            return True
        i = end
    return False


class FreezeAfterLoginProxy:
    """Acepta clientes, habla con PG real y deja de reenviar el SELECT."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target_host = target_host
        self.target_port = int(target_port)
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        serving = self._server.sockets
        assert serving
        self.port = int(serving[0].getsockname()[1])
        return self.port

    async def aclose(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in self._writers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        self._writers.clear()

    async def _handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        pg_reader, pg_writer = await asyncio.open_connection(
            self.target_host, self.target_port
        )
        self._writers.extend((client_writer, pg_writer))
        logged_in = asyncio.Event()
        frozen = asyncio.Event()

        async def client_to_server() -> None:
            try:
                while True:
                    data = await client_reader.read(65536)
                    if not data:
                        break
                    if frozen.is_set():
                        continue
                    # Tras el login, no reenviar la primera consulta (SELECT 1).
                    if logged_in.is_set() and (b"SELECT" in data or b"select" in data):
                        frozen.set()
                        continue
                    pg_writer.write(data)
                    await pg_writer.drain()
            except Exception:
                return
            finally:
                pg_writer.close()

        async def server_to_client() -> None:
            buf = b""
            try:
                while True:
                    data = await pg_reader.read(65536)
                    if not data:
                        break
                    if frozen.is_set():
                        continue
                    client_writer.write(data)
                    await client_writer.drain()
                    buf += data
                    if _buffer_has_ready_for_query(buf):
                        logged_in.set()
            except Exception:
                return
            finally:
                client_writer.close()

        await asyncio.gather(client_to_server(), server_to_client())
