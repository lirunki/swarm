"""Provider-free Windows integration check; run with the installed Cygwin Python.

Checks native Node's TTY, a native session/report round-trip, tmux lifetime,
terminal I/O/resize/detach, controller restart, exact-session recovery and stop.
Uses a temporary registry and a fake Pi entry point; no provider credentials,
real Pi sessions, or existing tmux servers are modified.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from swarm.controller import Controller
from swarm.models import CreateAgent
from swarm.paths import agent_path, cygwin
from swarm.processes import Processes
from swarm.store import Store
from swarm.terminal import bridge


def wait_for(test, description: str):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = test()
        if result:
            return result
        time.sleep(0.1)
    raise RuntimeError(f"Timed out: {description}")


async def terminal_check(processes, agent):
    class Browser:
        def __init__(self):
            self.output = b""
            self.messages = asyncio.Queue()
            self.messages.put_nowait(json.dumps({"type": "resize", "cols": 97, "rows": 26}))
            self.messages.put_nowait(json.dumps({"type": "input", "data": "swarm-smoke-input\r"}))
            self.replied = asyncio.Event()

        async def receive_text(self):
            return await self.messages.get()

        async def send_bytes(self, data):
            self.output += data
            if b"REPLY:swarm-smoke-input" in self.output:
                self.replied.set()

        async def close(self):
            pass

    browser = Browser()
    task = asyncio.create_task(bridge(browser, processes, agent, 100, 30))
    try:
        await asyncio.wait_for(browser.replied.wait(), 20)
    except TimeoutError:
        print("Terminal bridge output:", repr(browser.output[-8000:]), flush=True)
        print("Terminal bridge task finished:", task.done(), flush=True)
        if task.done() and not task.cancelled():
            print("Terminal bridge exception:", repr(task.exception()), flush=True)
        pane = processes.run("capture-pane", "-p", "-t", processes.target(agent), check=False)
        print("Native worker pane:", pane.stdout, pane.stderr, flush=True)
        raise
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert processes.alive(agent), "terminal detach terminated the native worker"


def server_check(home: Path, agent_id: str):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    log_path = home / "smoke-server.log"
    with log_path.open("w+", encoding="utf-8") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "swarm",
                "--home",
                str(home),
                "serve",
                "--port",
                str(port),
                "--no-recover",
            ],
            stdout=log,
            stderr=log,
        )
        try:

            def ready():
                if server.poll() is not None:
                    raise RuntimeError("HTTP server exited: " + log_path.read_text())
                try:
                    with urllib.request.urlopen(url + "/api/state", timeout=2) as response:
                        return json.load(response)
                except (urllib.error.URLError, TimeoutError):
                    return None

            state = wait_for(ready, "loopback HTTP server")
            assert any(a["id"] == agent_id and a["alive"] for a in state["agents"])

            async def websocket():
                from websockets import connect

                async with connect(
                    f"ws://127.0.0.1:{port}/ws/terminal/{agent_id}",
                    origin=url,
                ) as ws:
                    await ws.send(json.dumps({"type": "resize", "cols": 94, "rows": 27}))
                    await ws.send(json.dumps({"type": "input", "data": "smoke-over-websocket\r"}))
                    data = b""
                    try:
                        async with asyncio.timeout(20):
                            while b"REPLY:smoke-over-websocket" not in data:
                                chunk = await ws.recv()
                                assert isinstance(chunk, bytes)
                                data += chunk
                    except TimeoutError:
                        print("WebSocket terminal output:", repr(data[-8000:]), flush=True)
                        print("HTTP server log:", log_path.read_text(), flush=True)
                        raise

            asyncio.run(websocket())
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)


def native_alive(pid: int) -> bool:
    # A Cygwin PID is not a Windows PID. Check the native process without ever
    # terminating a PID from a record or touching an unrelated process.
    command = f"[bool](Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue)"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env=os.environ | {"PSModuleAnalysisCachePath": "NUL"},
    )
    return result.stdout.strip().lower() == "true"


def main():
    if not cygwin():
        raise SystemExit("Run this check with Swarm's installed Cygwin Python, not Windows Python")
    if not os.environ.get("SWARM_WINDOWS_NODE"):
        raise SystemExit("Source the installed env.sh before running this check")
    original_entry = os.environ.get("SWARM_WINDOWS_PI_ENTRY")
    with tempfile.TemporaryDirectory(prefix="swarm-windows-smoke-") as tmp:
        folder = Path(tmp)
        fake = folder / "fake-pi.mjs"
        bash = json.dumps(agent_path("/bin/bash"))
        fake.write_text(
            """
import fs from 'node:fs';
import readline from 'node:readline';
import {execFileSync} from 'node:child_process';
const args = process.argv.slice(2);
const session = args[args.indexOf('--session') + 1];
if (!process.stdin.isTTY || !process.stdout.isTTY) {
    console.error('NATIVE-TTY-FAIL: native Node must see a console inside Cygwin tmux');
    process.exit(4);
}
fs.writeFileSync(session + '.native-pid', String(process.pid));
if (!fs.existsSync(session)) fs.writeFileSync(session, JSON.stringify({
    type:'session', version:3, id:crypto.randomUUID(), cwd:process.cwd()
}) + '\\n');
execFileSync(BASH, ['-c', '"$SWARM_PYTHON" -m swarm report --status idle --notes "Windows smoke report"'], {
    env: {...process.env, PI_SESSION_FILE:session,
          PI_SESSION_ID:JSON.parse(fs.readFileSync(session,'utf8').split('\\n')[0]).id},
    stdio: ['ignore', 'ignore', 'inherit']
});
console.log('NATIVE-TTY-READY');
readline.createInterface({input:process.stdin}).on('line', line => console.log('REPLY:' + line));
""".replace("BASH", bash),
            encoding="utf-8",
        )
        os.environ["SWARM_WINDOWS_PI_ENTRY"] = str(fake)
        store = Store(folder / "registry")
        processes = Processes(store)
        controller = Controller(store, processes)
        try:
            agent = controller.create(
                CreateAgent(
                    name="Windows smoke", goal="Test Windows compatibility", folder=str(folder)
                )
            )
            if agent.last_error:
                raise RuntimeError(agent.last_error)
            wait_for(lambda: store.get(agent.id).notes == "Windows smoke report", "native report")
            controller.tick()
            agent = store.get(agent.id)
            assert agent.session_id, "native session UUID not captured"
            assert agent.session_file == str(store.sessions_dir / f"{agent.id}.jsonl")
            print("PASS: native Node TTY, exact session and report round-trip", flush=True)
            asyncio.run(terminal_check(processes, agent))
            first_pid = processes.pid(agent)
            server_check(store.home, str(agent.id))
            assert processes.alive(agent), "HTTP server shutdown terminated the worker"
            print("PASS: real HTTP/WebSocket terminal and server shutdown survival", flush=True)
            restarted = Controller(Store(store.home), Processes(Store(store.home)))
            restarted.recover()
            assert processes.pid(agent) == first_pid, "controller restart relaunched a live agent"
            print("PASS: terminal input/resize/detach and controller restart survival", flush=True)
            original = Path(agent.session_file).read_bytes()
            native_pid = int(Path(agent.session_file + ".native-pid").read_text())
            processes.stop(agent)
            wait_for(lambda: not native_alive(native_pid), "native worker exit after stop")
            store.update(agent.id, {"notes": ""})
            restarted.recover()
            wait_for(lambda: processes.alive(agent), "agent recovery")
            wait_for(lambda: store.get(agent.id).notes == "Windows smoke report", "recovery report")
            assert Path(agent.session_file).read_bytes() == original
            assert store.get(agent.id).session_id == agent.session_id
            native_pid = int(Path(agent.session_file + ".native-pid").read_text())
            restarted.stop(str(agent.id))
            assert not processes.alive(agent)
            wait_for(lambda: not native_alive(native_pid), "recovered native worker exit")
            print("PASS: exact-session recovery and owned-process stop", flush=True)
        finally:
            processes.run("kill-server", check=False)
            if "agent" in locals():
                native_file = Path(agent.session_file + ".native-pid")
                if native_file.exists():
                    wait_for(
                        lambda: not native_alive(int(native_file.read_text())),
                        "native worker cleanup before deleting its Windows cwd",
                    )
            if original_entry is None:
                os.environ.pop("SWARM_WINDOWS_PI_ENTRY", None)
            else:
                os.environ["SWARM_WINDOWS_PI_ENTRY"] = original_entry
    print("Windows integration smoke passed (no provider calls).", flush=True)


if __name__ == "__main__":
    main()
