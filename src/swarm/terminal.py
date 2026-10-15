"""Async PTY bridge to a disposable tmux *client*, never to the agent itself."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import struct
import termios

from fastapi import WebSocket, WebSocketDisconnect

from .models import Agent
from .processes import Processes


def dimensions(cols: int, rows: int) -> tuple[int, int]:
    return max(20, min(cols, 500)), max(5, min(rows, 200))


def resize(fd: int, cols: int, rows: int) -> None:
    cols, rows = dimensions(cols, rows)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def ready(fd: int, write: bool = False) -> None:
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def callback() -> None:
        if not future.done():
            future.set_result(None)

    add, remove = (
        (loop.add_writer, loop.remove_writer) if write else (loop.add_reader, loop.remove_reader)
    )
    add(fd, callback)
    try:
        await future
    finally:
        remove(fd)


async def bridge(ws: WebSocket, processes: Processes, agent: Agent, cols: int, rows: int) -> None:
    master, slave = os.openpty()
    resize(slave, cols, rows)
    os.set_blocking(master, False)
    env = dict(os.environ, TERM="xterm-256color")
    env.pop("TMUX", None)
    proc = None
    tasks = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *processes.attach_argv(agent),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        slave = -1

        async def output() -> None:
            while True:
                await ready(master)
                try:
                    data = os.read(master, 65536)
                except (OSError, BlockingIOError):
                    return
                if not data:
                    return
                await ws.send_bytes(data)

        async def input_() -> None:
            while True:
                message = json.loads(await ws.receive_text())
                if message.get("type") == "resize":
                    resize(master, int(message["cols"]), int(message["rows"]))
                elif message.get("type") == "input":
                    data = str(message["data"]).encode("utf-8")
                    if len(data) > 131072:
                        raise ValueError("input frame too large")
                    while data:
                        try:
                            count = os.write(master, data)
                            data = data[count:]
                        except BlockingIOError:
                            await ready(master, write=True)

        tasks = [
            asyncio.create_task(output()),
            asyncio.create_task(input_()),
            asyncio.create_task(proc.wait()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, OSError, ValueError, KeyError, TypeError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if slave >= 0:
            os.close(slave)
        os.close(master)
        # Detach/terminate only the attachment client. The tmux server owns the agent.
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 2)
            except (TimeoutError, ProcessLookupError):
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        try:
            await ws.close()
        except (WebSocketDisconnect, RuntimeError):
            pass
