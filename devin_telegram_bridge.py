"""Telegram <-> Devin bridge: chat with a Devin session from your Telegram bot (stdlib only).

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DEVIN_API_KEY (service user key, cog_...), DEVIN_ORG_ID.
Optional: DEVIN_SESSION_ID (attach to an existing session), DEVIN_MAX_ACU, BRIDGE_STATE_PATH, BRIDGE_POLL_SECONDS.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from telegram_chat import TelegramChat

DEVIN_API = "https://api.devin.ai/v3/organizations/{org}"
STATE_PATH = os.getenv("BRIDGE_STATE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         "devin_bridge_state.json"))
POLL_SECONDS = float(os.getenv("BRIDGE_POLL_SECONDS", "5"))
NEW_SESSION_PREAMBLE = (
    "You are talking to the user through their Telegram bot. Every message you send with message_user is "
    "forwarded to their Telegram chat, and their Telegram replies come back to you as messages. Reply in casual "
    "Indonesian (gw/lo), keep replies short and chatty like a person, and never block silently. Their main "
    "project is github.com/jekglory/verified-agent-identity (SOL dip bot on OKX Onchain OS).\n\nUser: "
)
HELP = ("Semua pesan diterusin ke Devin.\n/new <pesan> - mulai session Devin baru\n"
        "/session - link session sekarang\n/help - bantuan")


class DevinClient:
    def __init__(self, api_key: str, org_id: str):
        self.api_key = api_key
        self.base = DEVIN_API.format(org=org_id)

    def request(self, method: str, path: str, payload: dict | None = None, query: dict | None = None) -> dict:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Devin API {exc.code}: {exc.read().decode(errors='replace')[:300]}") from None

    def create_session(self, prompt: str, max_acu: int | None = None) -> dict:
        payload: dict = {"prompt": prompt, "title": "Telegram chat", "tags": ["telegram"]}
        if max_acu:
            payload["max_acu_limit"] = max_acu
        return self.request("POST", "/sessions", payload)

    def send(self, devin_id: str, message: str) -> dict:
        return self.request("POST", f"/sessions/{devin_id}/messages", {"message": message})

    def messages(self, devin_id: str, after: str | None) -> tuple[list[dict], str | None]:
        items: list[dict] = []
        while True:
            query = {"first": 100, **({"after": after} if after else {})}
            page = self.request("GET", f"/sessions/{devin_id}/messages", query=query)
            items += page.get("items", [])
            after = page.get("end_cursor") or after
            if not page.get("has_next_page"):
                return items, after


class Bridge:
    def __init__(self, devin: DevinClient, chat: TelegramChat, state_path: str = STATE_PATH,
                 max_acu: int | None = None):
        self.devin = devin
        self.chat = chat
        self.state_path = state_path
        self.max_acu = max_acu
        self.lock = threading.Lock()
        self.state = {"session_id": "", "url": "", "cursor": None, "seen": []}
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as fh:
                self.state.update(json.load(fh))

    def save(self) -> None:
        self.state["seen"] = self.state["seen"][-500:]
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)
        os.replace(tmp, self.state_path)

    def attach(self, devin_id: str, url: str = "") -> None:
        """Point the bridge at a session and skip its existing history."""
        with self.lock:
            self.state.update(session_id=devin_id, url=url or f"https://app.devin.ai/sessions/{devin_id[6:]}",
                              cursor=None, seen=[])
            items, cursor = self.devin.messages(devin_id, None)
            self.state["cursor"] = cursor
            self.state["seen"] = [m["event_id"] for m in items]
            self.save()

    def use_default(self, devin_id: str) -> None:
        """Attach to DEVIN_SESSION_ID once; a later /new session survives restarts until the default changes."""
        if not devin_id or devin_id == self.state.get("default_session"):
            return
        self.attach(devin_id)
        with self.lock:
            self.state["default_session"] = devin_id
            self.save()

    def on_command(self, name: str, args: str) -> str:
        if name in ("start", "help"):
            return HELP
        if name == "session":
            return self.state["url"] or "Belum ada session. Kirim pesan apa aja buat mulai."
        if name == "new":
            return self.start_session(args or "Halo")
        return self.forward(f"/{name} {args}".strip())

    def on_text(self, text: str, _history: list[dict]) -> str:
        return self.forward(text)

    def start_session(self, text: str) -> str:
        created = self.devin.create_session(NEW_SESSION_PREAMBLE + text, self.max_acu)
        with self.lock:
            self.state.update(session_id=created["session_id"], url=created["url"], cursor=None, seen=[])
            self.save()
        return f"Session Devin baru: {created['url']}\nTunggu balasan ya..."

    def forward(self, text: str) -> str:
        if not self.state["session_id"]:
            return self.start_session(text)
        self.devin.send(self.state["session_id"], text)
        self.chat.api("sendChatAction", {"chat_id": self.chat.chat_id, "action": "typing"}, timeout=15)
        return ""

    def poll_once(self) -> int:
        with self.lock:
            devin_id = self.state["session_id"]
            if not devin_id:
                return 0
            items, cursor = self.devin.messages(devin_id, self.state["cursor"])
            self.state["cursor"] = cursor
            seen = set(self.state["seen"])
            fresh = [m for m in items if m["event_id"] not in seen]
            self.state["seen"] += [m["event_id"] for m in fresh]
            if fresh:
                self.save()
        replies = [m["message"] for m in fresh if m.get("source") == "devin" and m.get("message")]
        for text in replies:
            self.chat.send(text)
        return len(replies)

    def run(self) -> None:
        self.chat.start()
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                print(f"Devin poll error: {exc}", flush=True)
            time.sleep(POLL_SECONDS)


def main() -> None:
    devin = DevinClient(os.environ["DEVIN_API_KEY"], os.environ["DEVIN_ORG_ID"])
    chat = TelegramChat(os.environ["TELEGRAM_BOT_TOKEN"], os.getenv("TELEGRAM_CHAT_ID", ""),
                        on_command=lambda n, a: "", log=lambda m: print(m, flush=True))
    max_acu = int(os.environ["DEVIN_MAX_ACU"]) if os.getenv("DEVIN_MAX_ACU") else None
    bridge = Bridge(devin, chat, max_acu=max_acu)
    chat.on_command, chat.on_text = bridge.on_command, bridge.on_text
    bridge.use_default(os.getenv("DEVIN_SESSION_ID", ""))
    print(f"Devin bridge running | session {bridge.state['session_id'] or '(new on first message)'}", flush=True)
    bridge.run()


if __name__ == "__main__":
    main()
