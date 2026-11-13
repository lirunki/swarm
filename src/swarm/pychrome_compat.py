"""Narrow pychrome 0.2.4 compatibility fix from EdgeMCP c09dc257."""
from __future__ import annotations

import warnings
from typing import Any

_PATCH_MARKER = "_edgemcp_empty_websocket_eof"


def install(pychrome: Any | None = None) -> None:
    """Treat websocket-client's empty close frame as EOF in pychrome's recv loop."""
    if pychrome is None:
        import pychrome as pychrome_module
        pychrome = pychrome_module
    tab_module = pychrome.tab
    current = tab_module.Tab._recv_loop
    if getattr(current, _PATCH_MARKER, False):
        return

    def _recv_loop(self):
        while not self._stopped.is_set():
            try:
                self._ws.settimeout(1)
                message_json = self._ws.recv()
                if message_json in ("", b""):
                    self._stopped.set()
                    return
                message = tab_module.json.loads(message_json)
            except tab_module.websocket.WebSocketTimeoutException:
                continue
            except (tab_module.websocket.WebSocketException, OSError):
                if not self._stopped.is_set():
                    tab_module.logger.error("websocket exception", exc_info=True)
                    self._stopped.set()
                return
            if self.debug:  # pragma: no cover
                print(f"< RECV {message_json}")
            if "method" in message:
                self.event_queue.put(message)
            elif "id" in message:
                if message["id"] in self.method_results:
                    self.method_results[message["id"]].put(message)
            else:  # pragma: no cover
                warnings.warn(f"unknown message: {message}")

    setattr(_recv_loop, _PATCH_MARKER, True)
    tab_module.Tab._recv_loop = _recv_loop
