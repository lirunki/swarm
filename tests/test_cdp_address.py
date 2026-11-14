import json
from types import SimpleNamespace

import pytest
import requests

from swarm import edgemcp_launcher, pychrome_compat
from swarm.edgemcp_browser import EdgeBrowser


@pytest.mark.parametrize(
    ("supplied", "canonical"),
    [
        ("localhost", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("10.20.30.40", "10.20.30.40"),
        ("2001:0db8::1", "2001:db8::1"),
        ("fe80::1", "fe80::1"),
    ],
)
def test_canonical_cdp_host_accepts_explicit_unicast_addresses(supplied, canonical):
    assert edgemcp_launcher.canonical_cdp_host(supplied) == canonical


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "example.com",
        "LOCALHOST",
        "http://127.0.0.1",
        "127.0.0.1/path",
        "user@127.0.0.1",
        "127.0.0.1\n",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "fe80::1%eth0",
        "255.255.255.255",
    ],
)
def test_canonical_cdp_host_rejects_non_ip_or_non_unicast_values(host):
    with pytest.raises(ValueError, match="CDP host"):
        edgemcp_launcher.canonical_cdp_host(host)


class _Response:
    def __init__(self, endpoint, payload):
        self.endpoint = endpoint
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def geturl(self):
        return self.endpoint

    def read(self, _limit):
        return json.dumps(self.payload).encode()


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


def test_probe_remote_identity_uses_bracketed_ipv6_and_accepts_loopback_advertisement(
    monkeypatch,
):
    browser_id = "66666666-6666-4666-8666-666666666666"
    connected = []
    opened = []
    monkeypatch.setattr(
        edgemcp_launcher.socket,
        "create_connection",
        lambda address, timeout: connected.append((address, timeout)) or _Connection(),
    )

    class Opener:
        def open(self, endpoint, timeout):
            opened.append((endpoint, timeout))
            return _Response(endpoint, {
                "Browser": "Chromium/123",
                "webSocketDebuggerUrl": (
                    f"ws://localhost:9222/devtools/browser/{browser_id}"
                ),
            })

    monkeypatch.setattr(edgemcp_launcher.urllib.request, "build_opener", lambda *_args: Opener())

    assert edgemcp_launcher.probe_identity(9222, host="2001:db8::7") == {
        "browser": "Chromium/123", "browser_id": browser_id,
    }
    assert connected == [(('2001:db8::7', 9222), 1)]
    assert opened == [("http://[2001:db8::7]:9222/json/version", 1)]


def test_probe_rejects_foreign_or_wrong_port_browser_websocket(monkeypatch):
    browser_id = "66666666-6666-4666-8666-666666666666"
    monkeypatch.setattr(
        edgemcp_launcher.socket, "create_connection", lambda *_args, **_kwargs: _Connection()
    )

    advertised = "ws://10.0.0.9:9222"

    class Opener:
        def open(self, endpoint, timeout):
            return _Response(endpoint, {
                "Browser": "Chromium/123",
                "webSocketDebuggerUrl": f"{advertised}/devtools/browser/{browser_id}",
            })

    monkeypatch.setattr(edgemcp_launcher.urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(RuntimeError, match="not a ready browser CDP endpoint"):
        edgemcp_launcher.probe_identity(9222, host="10.0.0.8")

    advertised = "ws://127.0.0.1:9223"
    with pytest.raises(RuntimeError, match="not a ready browser CDP endpoint"):
        edgemcp_launcher.probe_identity(9222, host="10.0.0.8")


class _Tab:
    def __init__(self, websocket):
        self.id = "page-one"
        self.type = "page"
        self._kwargs = {"webSocketDebuggerUrl": websocket}
        self._websocket_url = websocket


class _PychromeBrowser:
    def __init__(self, tab):
        self.tab = tab

    def list_tab(self, timeout=None):
        return [self.tab]


class _Pychrome:
    def __init__(self, tab):
        self.tab = tab
        self.urls = []

    def Browser(self, url):
        self.urls.append(url)
        return _PychromeBrowser(self.tab)


def test_real_pychrome_http_sessions_are_isolated_without_global_mutation(monkeypatch):
    import pychrome

    seen = []
    original_requests = pychrome.browser.requests

    def request(_session, method, url, **kwargs):
        seen.append((method, url, kwargs))
        payload = {"Browser": "Chromium/123"} if url.endswith("/json/version") else []
        return SimpleNamespace(status_code=200, json=lambda: payload, text="ok")

    monkeypatch.setattr(requests.Session, "request", request)
    first = EdgeBrowser(9222, pychrome, host="10.0.0.8")._get_browser()
    second = EdgeBrowser(9333, pychrome, host="10.0.0.9")._get_browser()

    assert first.list_tab(timeout=5) == []
    assert second.version(timeout=3) == {"Browser": "Chromium/123"}
    assert first.session is not second.session
    assert first.session.trust_env is second.session.trust_env is False
    assert pychrome.browser.requests is original_requests
    assert seen == [
        ("GET", "http://10.0.0.8:9222/json", {
            "allow_redirects": False, "timeout": 5,
        }),
        ("GET", "http://10.0.0.9:9333/json/version", {
            "allow_redirects": False, "json": True, "timeout": 3,
        }),
    ]


def test_real_pychrome_adapter_rejects_redirect_status(monkeypatch):
    import pychrome

    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=302),
    )
    browser = EdgeBrowser(9222, pychrome, host="10.0.0.8")._get_browser()

    with pytest.raises(requests.HTTPError, match="status 302"):
        browser.list_tab(timeout=5)


def test_edge_browser_rebases_loopback_page_websocket_to_requested_host():
    tab = _Tab("ws://127.0.0.1:9222/devtools/page/page-one")
    pychrome = _Pychrome(tab)
    browser = EdgeBrowser(9222, pychrome, host="10.0.0.8")

    assert browser._validated_tabs() == [tab]
    assert pychrome.urls == ["http://10.0.0.8:9222"]
    assert tab._websocket_url == "ws://10.0.0.8:9222/devtools/page/page-one"
    assert not hasattr(browser._browser, "session")


@pytest.mark.parametrize(
    "websocket",
    [
        "ws://10.0.0.9:9222/devtools/page/page-one",
        "ws://127.0.0.1:9223/devtools/page/page-one",
        "ws://127.0.0.1:9222/devtools/page/another-page",
        "ws://user@127.0.0.1:9222/devtools/page/page-one",
        "ws://127.0.0.1:9222/devtools/page/page-one?next=foreign",
    ],
)
def test_edge_browser_rejects_foreign_page_websocket_before_start(websocket):
    browser = EdgeBrowser(9222, _Pychrome(_Tab(websocket)), host="10.0.0.8")
    with pytest.raises(ValueError, match="websocket"):
        browser._validated_tabs()


def test_pychrome_websocket_start_explicitly_disables_proxy():
    calls = []
    fake_websocket = SimpleNamespace(create_connection=lambda url, **kwargs: calls.append((url, kwargs)))

    class Tab:
        status_started = "started"

        def _recv_loop(self):
            return None

        def start(self):
            return None

    fake = SimpleNamespace(
        tab=SimpleNamespace(
            Tab=Tab,
            RuntimeException=RuntimeError,
            websocket=fake_websocket,
            json=json,
            logger=SimpleNamespace(error=lambda *_args, **_kwargs: None),
        )
    )
    pychrome_compat.install(fake)
    tab = Tab()
    tab._started = False
    tab._websocket_url = "ws://10.0.0.8:9222/devtools/page/page-one"
    tab._stopped = SimpleNamespace(clear=lambda: None, is_set=lambda: False)
    tab._recv_th = SimpleNamespace(start=lambda: None)
    tab._handle_event_th = SimpleNamespace(start=lambda: None)

    tab.start()

    assert calls[0][1]["http_proxy_host"] is None
    assert calls[0][1]["http_no_proxy"] == ["*"]
