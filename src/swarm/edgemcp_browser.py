"""Thin pychrome extraction of EdgeMCP's Remote Web browser controls.

Adapted from EdgeMCP server.py at commit
c09dc257886305539ea90bc454ceda63b9b150a6. It intentionally contains no MCP,
HTTP server, SSH, or general-purpose JavaScript evaluation interface.
"""
from __future__ import annotations

import base64
import functools
import json
import threading
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .edgemcp_launcher import (
    _allowed_advertised_host,
    _http_authority,
    _validate_port,
    canonical_cdp_host,
)

_CDP_KEYS = {
    "Enter": ("Enter", "Enter", 13, "\r", 0), "Tab": ("Tab", "Tab", 9, "\t", 0),
    "Escape": ("Escape", "Escape", 27, "", 0),
    "Backspace": ("Backspace", "Backspace", 8, "\b", 0),
    "Delete": ("Delete", "Delete", 46, "\x7f", 0), "Space": ("Space", " ", 32, " ", 0),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, "", 0),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, "", 0),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, "", 0),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, "", 0),
    "Home": ("Home", "Home", 36, "", 0), "End": ("End", "End", 35, "", 0),
    "PageUp": ("PageUp", "PageUp", 33, "", 0),
    "PageDown": ("PageDown", "PageDown", 34, "", 0),
}
for _number in range(1, 13):
    _CDP_KEYS[f"F{_number}"] = (f"F{_number}", f"F{_number}", 111 + _number, "", 0)
_MOD_BITS = {"ctrl": 2, "alt": 1, "shift": 16, "meta": 8}
_COPY_EXPRESSION = (
    "(()=>{const e=document.activeElement;"
    "if(e&&e.tagName==='INPUT'&&e.type==='password')return '';"
    "if(e&&(e.tagName==='INPUT'||e.tagName==='TEXTAREA')&&"
    "typeof e.selectionStart==='number')return e.value.slice(e.selectionStart,e.selectionEnd);"
    "return String(window.getSelection()||'')})()"
)


def _serialized(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return function(self, *args, **kwargs)
    return wrapped


class EdgeBrowser:
    """The existing EdgeMCP selected-tab controls, scoped to one CDP port."""

    def __init__(
        self, port: int, pychrome_module: Any | None = None, *, host: str = "127.0.0.1"
    ):
        self.port = _validate_port(port)
        self.host = canonical_cdp_host(host)
        self._pychrome = pychrome_module
        self._browser = None
        self._tab = None
        self._contexts: dict[str, int] = {}
        self._reverse_contexts: dict[int, str] = {}
        self._viewport: tuple[str, int, int] | None = None
        self._lock = threading.RLock()

    def _module(self):
        if self._pychrome is None:
            import pychrome

            from . import pychrome_compat
            pychrome_compat.install(pychrome)
            self._pychrome = pychrome
        return self._pychrome

    def _get_browser(self):
        if self._browser is None:
            module = self._module()
            browser_type = module.Browser
            browser_module = getattr(module, "browser", None)
            if (
                browser_module is not None
                and getattr(browser_module, "Browser", None) is browser_type
                and hasattr(browser_module, "Tab")
            ):
                import requests

                tab_type = browser_module.Tab

                class SessionBrowser(browser_type):
                    def __init__(browser, url):
                        super().__init__(url=url)
                        browser.session = requests.Session()
                        browser.session.trust_env = False

                    def _request(browser, method, path, **kwargs):
                        response = browser.session.request(
                            method, f"{browser.dev_url}{path}", allow_redirects=False,
                            **kwargs,
                        )
                        if not 200 <= response.status_code < 300:
                            raise requests.HTTPError(
                                f"Unexpected CDP HTTP status {response.status_code}",
                                response=response,
                            )
                        return response

                    def list_tab(browser, timeout=None):
                        payload = browser._request("GET", "/json", timeout=timeout).json()
                        if not isinstance(payload, list):
                            raise ValueError("Invalid browser target list")
                        tabs = {}
                        for metadata in payload:
                            if not isinstance(metadata, dict) or metadata.get("type") != "page":
                                continue
                            target_id = metadata.get("id")
                            existing = browser._tabs.get(target_id)
                            tabs[target_id] = (
                                existing if existing is not None
                                and existing.status != tab_type.status_stopped
                                else tab_type(**metadata)
                            )
                        browser._tabs = tabs
                        return list(tabs.values())

                    def new_tab(browser, url=None, timeout=None):
                        payload = browser._request(
                            "PUT", f"/json/new?{url or ''}", json=True, timeout=timeout
                        ).json()
                        if not isinstance(payload, dict):
                            raise ValueError("Invalid new browser target")
                        tab = tab_type(**payload)
                        browser._tabs[tab.id] = tab
                        return tab

                    def activate_tab(browser, tab_id, timeout=None):
                        tab_id = tab_id.id if isinstance(tab_id, tab_type) else tab_id
                        return browser._request(
                            "GET", f"/json/activate/{tab_id}", timeout=timeout
                        ).text

                    def close_tab(browser, tab_id, timeout=None):
                        tab_id = tab_id.id if isinstance(tab_id, tab_type) else tab_id
                        tab = browser._tabs.pop(tab_id, None)
                        if tab is not None and tab.status == tab_type.status_started:
                            tab.stop()
                        return browser._request(
                            "GET", f"/json/close/{tab_id}", timeout=timeout
                        ).text

                    def version(browser, timeout=None):
                        payload = browser._request(
                            "GET", "/json/version", json=True, timeout=timeout
                        ).json()
                        if not isinstance(payload, dict):
                            raise ValueError("Invalid browser version")
                        return payload

                browser_type = SessionBrowser
            self._browser = browser_type(
                url=f"http://{_http_authority(self.host, self.port)}"
            )
        return self._browser

    def _validated_tabs(self) -> list[Any]:
        tabs = self._get_browser().list_tab(timeout=5)
        for tab in tabs:
            metadata = tab._kwargs if "_kwargs" in vars(tab) else {}
            websocket = metadata.get("webSocketDebuggerUrl") if isinstance(metadata, dict) else None
            if websocket is None:
                continue
            try:
                parsed = urlsplit(websocket)
                expected_path = f"/devtools/page/{tab.id}"
                valid = (
                    parsed.scheme == "ws"
                    and _allowed_advertised_host(parsed.hostname, self.host)
                    and parsed.port == self.port
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.path == expected_path
                    and not parsed.query
                    and not parsed.fragment
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError("Invalid page websocket URL")
            rebased = urlunsplit((
                "ws", _http_authority(self.host, self.port), expected_path, "", ""
            ))
            metadata["webSocketDebuggerUrl"] = rebased
            if hasattr(tab, "_websocket_url"):
                tab._websocket_url = rebased
        return tabs

    @staticmethod
    def _page(metadata: Any) -> dict | None:
        if not isinstance(metadata, dict) or metadata.get("type", "page") != "page":
            return None
        target_id = metadata.get("id")
        if not isinstance(target_id, str) or not target_id:
            return None
        url, title = metadata.get("url", ""), metadata.get("title", "")
        return {
            "id": target_id,
            "url": url[:4096] if isinstance(url, str) else "",
            "title": title[:4096] if isinstance(title, str) else "",
        }

    def _list_pages(self) -> list[dict]:
        """Fetch current target metadata without proxies or redirects."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        endpoint = f"http://{_http_authority(self.host, self.port)}/json"
        with opener.open(endpoint, timeout=5) as response:
            if response.geturl() != endpoint:
                raise ValueError("Redirected browser target list")
            content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Oversized browser target list")
        targets = json.loads(content)
        if not isinstance(targets, list):
            raise ValueError("Invalid browser target list")
        return [page for target in targets if (page := self._page(target)) is not None]

    def _pages(self) -> list[dict]:
        tabs = self._validated_tabs()
        try:
            return self._list_pages()
        except (OSError, UnicodeError, ValueError):
            pages = []
            for tab in tabs:
                if getattr(tab, "type", "page") != "page":
                    continue
                # pychrome keeps /json constructor data in _kwargs. Accessing
                # tab.url/tab.title manufactures non-serializable GenericAttr objects.
                metadata = tab._kwargs if "_kwargs" in vars(tab) else {}
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata = {**metadata, "id": tab.id, "type": tab.type}
                page = self._page(metadata)
                if page is not None:
                    pages.append(page)
            return pages

    def _find(self, target_id: str):
        for tab in self._validated_tabs():
            if tab.id == target_id:
                return tab
        raise RuntimeError(f"No page target with id {target_id}")

    def _select(self, target_id: str):
        self._detach_tab()
        self._contexts, self._reverse_contexts = {}, {}
        tab = self._find(target_id)
        tab.start()
        contexts, reverse = self._contexts, self._reverse_contexts

        def created(**payload):
            context = payload.get("context") or {}
            auxiliary = context.get("auxData") or {}
            frame_id, context_id = auxiliary.get("frameId"), context.get("id")
            if frame_id and context_id and auxiliary.get("isDefault"):
                contexts[frame_id] = context_id
                reverse[context_id] = frame_id

        def destroyed(**payload):
            context_id = payload.get("executionContextId")
            frame_id = reverse.pop(context_id, None)
            if frame_id is not None and contexts.get(frame_id) == context_id:
                contexts.pop(frame_id, None)

        def cleared(**_payload):
            contexts.clear()
            reverse.clear()

        tab.Runtime.executionContextCreated = created
        tab.Runtime.executionContextDestroyed = destroyed
        tab.Runtime.executionContextsCleared = cleared
        try:
            tab.Page.enable(_timeout=5)
            tab.Runtime.enable(_timeout=5)
        except Exception:
            tab.stop()
            raise
        self._tab = tab
        return tab

    def _detach_tab(self):
        if self._tab is not None:
            try:
                self._tab.stop()
            except Exception:
                pass
            self._tab = None
        self._viewport = None

    def _target(self, target_id: str):
        if self._tab is None or self._tab.id != target_id:
            self._select(target_id)
        try:
            self._tab.Runtime.evaluate(expression="1", _timeout=3)
        except Exception:
            self._select(target_id)
        return self._tab

    def _eval(self, expression: str):
        result = self._tab.Runtime.evaluate(
            expression=expression, returnByValue=True, awaitPromise=True,
            allowUnsafeEvalBlocking=True, _timeout=10,
        )
        if result and result.get("exceptionDetails"):
            raise RuntimeError("Browser evaluation failed")
        return ((result or {}).get("result") or {}).get("value")

    @_serialized
    def tabs(self) -> dict:
        return {
            "tabs": self._pages(),
            "selected": self._tab.id if self._tab is not None else None,
        }

    @_serialized
    def select(self, target_id: str) -> dict:
        self._select(target_id)
        self._get_browser().activate_tab(target_id, timeout=5)
        return {"ok": True}

    @_serialized
    def screenshot(self, target_id: str) -> bytes:
        tab = self._target(target_id)
        result = tab.Page.captureScreenshot(
            format="png", captureBeyondViewport=False, fromSurface=True, _timeout=10
        )
        return base64.b64decode(result["data"], validate=True)

    @_serialized
    def resize(self, target_id: str, width: int, height: int) -> dict:
        """Resize the selected browser window and set an exact page viewport."""
        tab = self._target(target_id)
        requested = (target_id, width, height)
        if self._viewport == requested:
            return {"resized": True, "width": width, "height": height}

        # Window bounds include browser chrome, so this is only a best-effort OS
        # resize. The Emulation command below is authoritative for layout and PNGs.
        call_error = getattr(self._module(), "CallMethodException", ())
        try:
            window = tab.Browser.getWindowForTarget(targetId=target_id, _timeout=5)
        except call_error:
            window = None
        window_id = window.get("windowId") if isinstance(window, dict) else None
        if type(window_id) is int:
            try:
                tab.Browser.setWindowBounds(
                    windowId=window_id, bounds={"windowState": "normal"}, _timeout=5
                )
            except call_error:
                pass
            try:
                tab.Browser.setWindowBounds(
                    windowId=window_id, bounds={"width": width, "height": height},
                    _timeout=5,
                )
            except call_error:
                pass

        tab.Emulation.setDeviceMetricsOverride(
            width=width, height=height, deviceScaleFactor=1, mobile=False, _timeout=5
        )
        self._viewport = requested
        return {"resized": True, "width": width, "height": height}

    @_serialized
    def click(self, target_id: str, x_ratio: float, y_ratio: float) -> dict:
        tab = self._target(target_id)
        viewport = self._eval("({width: window.innerWidth, height: window.innerHeight})")
        common = {
            "x": x_ratio * viewport["width"], "y": y_ratio * viewport["height"],
            "button": "left", "clickCount": 1,
        }
        tab.Input.dispatchMouseEvent(type="mousePressed", **common)
        tab.Input.dispatchMouseEvent(type="mouseReleased", **common)
        return {"ok": True}

    @_serialized
    def drag(self, target_id: str, start_x: float, start_y: float,
             end_x: float, end_y: float) -> dict:
        tab = self._target(target_id)
        viewport = self._eval("({width: window.innerWidth, height: window.innerHeight})")
        sx, sy = start_x * viewport["width"], start_y * viewport["height"]
        ex, ey = end_x * viewport["width"], end_y * viewport["height"]
        pressed = False
        try:
            tab.Input.dispatchMouseEvent(
                type="mousePressed", x=sx, y=sy, button="left", clickCount=1
            )
            pressed = True
            tab.Input.dispatchMouseEvent(
                type="mouseMoved", x=ex, y=ey, button="left", buttons=1
            )
        finally:
            if pressed:
                tab.Input.dispatchMouseEvent(
                    type="mouseReleased", x=ex, y=ey, button="left", clickCount=1
                )
        return {"ok": True}

    @_serialized
    def text(self, target_id: str, text: str) -> dict:
        self._target(target_id).Input.insertText(text=text)
        return {"ok": True}

    @_serialized
    def key(self, target_id: str, key: str, modifiers: list[str]) -> dict:
        tab = self._target(target_id)
        modifier_bits = 0
        for modifier in modifiers:
            modifier_bits |= _MOD_BITS[modifier]
        entry = _CDP_KEYS.get(key)
        if entry is None:
            entry = (
                key if len(key) == 1 and key.isalpha() else "Unidentified", key,
                ord(key.upper()) if len(key) == 1 and key.isalpha() else 0,
                key if len(key) == 1 else "", 0,
            )
        code, normalized_key, key_code, text, location = entry
        if modifier_bits & (_MOD_BITS["ctrl"] | _MOD_BITS["alt"] | _MOD_BITS["meta"]):
            text = ""
        common = {
            "key": normalized_key, "code": code, "windowsVirtualKeyCode": key_code,
            "nativeVirtualKeyCode": key_code, "modifiers": modifier_bits, "text": text,
            "location": location, "unmodifiedText": text, "isKeypad": False,
            "isSystemKey": False,
        }
        tab.Input.dispatchKeyEvent(type="keyDown", **common)
        if text:
            tab.Input.dispatchKeyEvent(type="char", **common)
        tab.Input.dispatchKeyEvent(type="keyUp", **common)
        return {"ok": True}

    @_serialized
    def copy(self, target_id: str) -> dict:
        self._target(target_id)
        return {"text": str(self._eval(_COPY_EXPRESSION) or "")[:4096]}

    @_serialized
    def new(self, url: str) -> dict:
        tab = self._get_browser().new_tab(url=url, timeout=10)
        self._select(tab.id)
        self._get_browser().activate_tab(tab.id, timeout=5)
        return {"id": tab.id, "url": url}

    @_serialized
    def close_tab(self, target_id: str) -> dict:
        if not any(page["id"] == target_id for page in self._pages()):
            raise ValueError(f"No page target with id {target_id}")
        self._get_browser().close_tab(target_id, timeout=10)
        if self._tab is not None and self._tab.id == target_id:
            self._detach_tab()
            self._contexts, self._reverse_contexts = {}, {}
        return {"ok": True}

    def _history(self, target_id: str, offset: int) -> dict:
        tab = self._target(target_id)
        history = tab.Page.getNavigationHistory(_timeout=5)
        entries = history.get("entries", [])
        destination = history.get("currentIndex", 0) + offset
        if 0 <= destination < len(entries):
            tab.Page.navigateToHistoryEntry(entryId=entries[destination]["id"], _timeout=10)
            try:
                tab.wait(timeout=5)
            except Exception:
                pass
        return {"ok": True}

    @_serialized
    def back(self, target_id: str) -> dict:
        return self._history(target_id, -1)

    @_serialized
    def forward(self, target_id: str) -> dict:
        return self._history(target_id, 1)

    @_serialized
    def reload(self, target_id: str) -> dict:
        tab = self._target(target_id)
        tab.Page.reload(ignoreCache=False, _timeout=10)
        try:
            tab.wait(timeout=5)
        except Exception:
            pass
        return {"ok": True}

    @_serialized
    def close(self) -> None:
        """Detach pychrome only; never stop the browser process."""
        self._detach_tab()
        self._browser = None
