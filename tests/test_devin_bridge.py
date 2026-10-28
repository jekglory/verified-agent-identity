import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devin_telegram_bridge import Bridge  # noqa: E402


class FakeDevin:
    def __init__(self):
        self.msgs, self.sent, self.created = [], [], []

    def create_session(self, prompt, max_acu=None):
        self.created.append(prompt)
        return {"session_id": "devin-abc", "url": "https://app.devin.ai/sessions/abc"}

    def send(self, devin_id, message):
        self.sent.append((devin_id, message))

    def messages(self, devin_id, after):
        return list(self.msgs), "c1"


class FakeChat:
    chat_id = "42"

    def __init__(self):
        self.out, self.calls = [], []

    def send(self, text, chat_id=None):
        self.out.append(text)

    def api(self, method, payload, timeout=60):
        self.calls.append(method)


def test_bridge_flow(tmp_path):
    devin, chat = FakeDevin(), FakeChat()
    bridge = Bridge(devin, chat, state_path=str(tmp_path / "s.json"))
    assert "app.devin.ai" in bridge.on_text("halo", [])
    assert devin.created and devin.created[0].endswith("halo")
    devin.msgs = [{"event_id": "e1", "source": "user", "message": "halo"},
                  {"event_id": "e2", "source": "devin", "message": "Halo bro!"}]
    assert bridge.poll_once() == 1 and chat.out == ["Halo bro!"]
    assert bridge.poll_once() == 0
    assert bridge.on_text("cek saldo", []) == ""
    assert devin.sent == [("devin-abc", "cek saldo")] and "sendChatAction" in chat.calls
    reloaded = Bridge(devin, chat, state_path=str(tmp_path / "s.json"))
    assert reloaded.state["session_id"] == "devin-abc" and reloaded.poll_once() == 0
    assert "app.devin.ai" in bridge.on_command("session", "")


def test_attach_skips_history(tmp_path):
    devin, chat = FakeDevin(), FakeChat()
    devin.msgs = [{"event_id": "old", "source": "devin", "message": "old reply"}]
    bridge = Bridge(devin, chat, state_path=str(tmp_path / "s.json"))
    bridge.attach("devin-xyz")
    assert bridge.poll_once() == 0 and chat.out == []
