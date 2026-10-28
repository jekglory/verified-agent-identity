"""Telegram long-polling listener with command routing and optional LLM chat (stdlib only)."""

import json
import os
import threading
import urllib.error
import urllib.request
from collections import deque
from typing import Callable

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4000
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"
# OpenAI-compatible chat endpoints: provider -> (url, default model)
OPENAI_COMPATIBLE = {
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-flash-lite-latest"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
}


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from None


def llm_provider(api_key: str) -> str:
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith(("AIza", "AQ.")):
        return "gemini"
    if api_key.startswith("gsk_"):
        return "groq"
    return "openai"


def ask_llm(api_key: str, system: str, messages: list[dict], model: str = "") -> str:
    """Send a chat to Anthropic, OpenAI, Gemini, or Groq (picked from the key prefix); return the reply."""
    provider = llm_provider(api_key)
    if provider == "anthropic":
        data = _post_json(
            "https://api.anthropic.com/v1/messages",
            {"model": model or DEFAULT_ANTHROPIC_MODEL, "max_tokens": 800, "system": system, "messages": messages},
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        return "".join(part.get("text", "") for part in data.get("content", [])).strip()
    url, default_model = OPENAI_COMPATIBLE[provider]
    data = _post_json(
        url,
        {"model": model or default_model, "max_tokens": 800,
         "messages": [{"role": "system", "content": system}, *messages]},
        {"Authorization": f"Bearer {api_key}"},
    )
    return data["choices"][0]["message"]["content"].strip()


class TelegramChat:
    """Polls getUpdates in a daemon thread and answers only the configured chat.

    on_command(name, args) handles "/name args" messages; on_text(text, history) handles free text.
    Both return the reply text. Without a chat ID, /start replies with the sender's chat ID.
    """

    def __init__(self, token: str, chat_id: str,
                 on_command: Callable[[str, str], str],
                 on_text: Callable[[str, list[dict]], str] | None = None,
                 log: Callable[[str], None] = print):
        self.token = token
        self.chat_id = str(chat_id)
        self.on_command = on_command
        self.on_text = on_text
        self.log = log
        self.history: deque[dict] = deque(maxlen=12)
        self.offset = 0
        self._stop = threading.Event()

    def api(self, method: str, payload: dict, timeout: int = 60) -> dict:
        return _post_json(TELEGRAM_API.format(token=self.token, method=method), payload, timeout=timeout)

    def send(self, text: str, chat_id: str | None = None) -> None:
        target = chat_id or self.chat_id
        if not target:
            return
        for i in range(0, max(len(text), 1), MAX_MESSAGE_LEN):
            self.api("sendMessage", {"chat_id": target, "text": text[i:i + MAX_MESSAGE_LEN] or "-"}, timeout=15)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="telegram", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = self.api("getUpdates", {"offset": self.offset, "timeout": 30,
                                                  "allowed_updates": ["message"]}, timeout=40)
                for update in updates.get("result", []):
                    self.offset = update["update_id"] + 1
                    self.handle_update(update)
            except Exception as exc:
                self.log(f"Telegram polling error: {exc}")
                self._stop.wait(5)

    def handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        sender = str(msg.get("chat", {}).get("id", ""))
        if not text or not sender:
            return
        if not self.chat_id:
            if text.startswith("/start"):
                self.log(f"Telegram /start from chat ID {sender}; set TELEGRAM_CHAT_ID to enable control")
                self.send(f"Chat ID kamu: {sender}\nSet TELEGRAM_CHAT_ID={sender} lalu restart bot.", sender)
            return
        if sender != self.chat_id:
            self.log(f"Ignored Telegram message from unauthorized chat {sender}")
            return
        try:
            reply = self.route(text)
        except Exception as exc:
            reply = f"Error: {exc}"
        if reply:
            self.send(reply)

    def route(self, text: str) -> str:
        if text.startswith("/"):
            head, _, args = text.partition(" ")
            name = head[1:].split("@", 1)[0].lower()
            return self.on_command(name, args.strip())
        if not self.on_text:
            return "Chat AI belum aktif (LLM_API_KEY belum di-set). Ketik /help untuk daftar perintah."
        self.history.append({"role": "user", "content": text})
        reply = self.on_text(text, list(self.history))
        self.history.append({"role": "assistant", "content": reply})
        return reply


def find_chat_ids(token: str) -> list[tuple[str, str]]:
    """Return (chat_id, name) pairs from pending updates; used once to discover TELEGRAM_CHAT_ID."""
    data = _post_json(TELEGRAM_API.format(token=token, method="getUpdates"), {"timeout": 0}, timeout=15)
    seen: dict[str, str] = {}
    for update in data.get("result", []):
        chat = (update.get("message") or {}).get("chat", {})
        if "id" in chat:
            seen[str(chat["id"])] = chat.get("username") or chat.get("first_name") or ""
    return list(seen.items())


if __name__ == "__main__":
    for cid, name in find_chat_ids(os.environ["TELEGRAM_BOT_TOKEN"]):
        print(cid, name)
