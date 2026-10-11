"""Discovery and the token option, against a fake Supervisor."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nova_voice_id import supervisor

TOKEN = "cd" * 32


class FakeSupervisor:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, str]] = []
        self.options = {"upstream_uri": "tcp://core-whisper:10300", "log_level": "warning"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, data):
                body = json.dumps({"result": "ok", "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(
                    (self.command, self.path, body, self.headers.get("Authorization"))
                )
                return body

            def do_GET(self):
                self._record()
                self._reply({"options": dict(outer.options)})

            def do_POST(self):
                body = self._record()
                if self.path == "/addons/self/options":
                    outer.options = body["options"]
                self._reply({"uuid": "x"})

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def fake_supervisor(monkeypatch):
    fake = FakeSupervisor()
    monkeypatch.setattr(supervisor, "SUPERVISOR_URL", fake.url)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret")
    yield fake
    fake.server.shutdown()


def test_discovery_sends_uri_and_token_to_service_nova(fake_supervisor, caplog):
    caplog.set_level(logging.DEBUG)
    supervisor.send_discovery("tcp://abc-nova-voice-id:10350", TOKEN)

    method, path, body, auth = fake_supervisor.requests[-1]
    assert (method, path) == ("POST", "/discovery")
    assert body == {
        "service": "nova",
        "config": {"uri": "tcp://abc-nova-voice-id:10350", "token": TOKEN},
    }
    assert auth == "Bearer supervisor-secret"
    assert TOKEN not in caplog.text


def test_token_is_copied_into_options_once(fake_supervisor):
    assert supervisor.show_token(TOKEN) is True
    assert fake_supervisor.options == {
        "upstream_uri": "tcp://core-whisper:10300",
        "log_level": "warning",
        "token": TOKEN,
    }
    posts = [r for r in fake_supervisor.requests if r[0] == "POST"]
    assert supervisor.show_token(TOKEN) is False  # already there: no second write
    assert [r for r in fake_supervisor.requests if r[0] == "POST"] == posts


def test_supervisor_errors_do_not_include_the_token(monkeypatch):
    monkeypatch.setattr(supervisor, "SUPERVISOR_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
    with pytest.raises(supervisor.SupervisorError) as err:
        supervisor.send_discovery("tcp://h:10350", TOKEN)
    assert TOKEN not in str(err.value)
