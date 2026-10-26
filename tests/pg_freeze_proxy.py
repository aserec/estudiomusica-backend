"""Proxy TCP async que congela tráfico PG después de inicializar el dialecto."""

from __future__ import annotations

import asyncio
import contextlib
import struct
from typing import Literal

FreezeOn = Literal["select_1", "pre_ping", "rollback", "connect", "none"]


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


def _client_sql(data: bytes) -> str:
    return data.decode("utf-8", "replace").lower()


class FreezeAfterLoginProxy:
    """Habla con PG real y congela SELECT 1, pre_ping o ROLLBACK de reset."""

    def __init__(
        self,
        target_host: str,
        target_port: int,
        *,
        freeze_on: FreezeOn = "select_1",
    ) -> None:
        self.target_host = target_host
        self.target_port = int(target_port)
        self.freeze_on: FreezeOn = freeze_on
        self.port = 0
        self.accepted_connections = 0
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

    def _should_freeze(
        self,
        data: bytes,
        *,
        logged_in: bool,
        dialect_ready: bool,
        seen_select_1: bool,
        first_cycle_done: bool,
    ) -> bool:
        sql = _client_sql(data)
        is_version = "pg_catalog.version" in sql or "version()" in sql
        is_select_1 = "select 1" in sql and not is_version
        is_rollback = "rollback" in sql
        is_empty_ping = ";" in sql and "select" not in sql and not is_rollback

        if self.freeze_on == "none":
            return False
        if self.freeze_on == "connect":
            return True
        if self.freeze_on == "select_1":
            return logged_in and is_select_1
        if self.freeze_on == "rollback":
            return logged_in and seen_select_1
        if self.freeze_on == "pre_ping":
            return first_cycle_done and (is_empty_ping or is_select_1 or is_rollback)
        return False

    async def _handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        self.accepted_connections += 1
        if self.freeze_on == "connect":
            self._writers.append(client_writer)
            try:
                while True:
                    data = await client_reader.read(65536)
                    if not data:
                        break
            except Exception:
                return
            finally:
                client_writer.close()
            return
        pg_reader, pg_writer = await asyncio.open_connection(
            self.target_host, self.target_port
        )
        self._writers.extend((client_writer, pg_writer))
        logged_in = asyncio.Event()
        frozen = asyncio.Event()
        dialect_ready = False
        seen_select_1 = False
        seen_reset = False
        first_cycle_done = False
        ready_count = 0

        async def client_to_server() -> None:
            nonlocal seen_select_1, seen_reset
            try:
                while True:
                    data = await client_reader.read(65536)
                    if not data:
                        break
                    if frozen.is_set():
                        continue
                    sql = _client_sql(data)
                    if self._should_freeze(
                        data,
                        logged_in=logged_in.is_set(),
                        dialect_ready=dialect_ready,
                        seen_select_1=seen_select_1,
                        first_cycle_done=first_cycle_done,
                    ):
                        frozen.set()
                        continue
                    if "select 1" in sql and "version" not in sql:
                        seen_select_1 = True
                    if "rollback" in sql and seen_select_1:
                        seen_reset = True
                    pg_writer.write(data)
                    await pg_writer.drain()
            except Exception:
                return
            finally:
                pg_writer.close()

        async def server_to_client() -> None:
            nonlocal dialect_ready, first_cycle_done, ready_count
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
                        ready_count += 1
                        logged_in.set()
                        # Auth = 1er Z; version() del dialecto = 2º Z.
                        if ready_count >= 2:
                            dialect_ready = True
                        if seen_reset:
                            first_cycle_done = True
                        buf = b""
            except Exception:
                return
            finally:
                client_writer.close()

        await asyncio.gather(client_to_server(), server_to_client())
