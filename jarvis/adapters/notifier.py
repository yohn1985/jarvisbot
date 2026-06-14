"""Notifier adapters — how Jarvis reaches the owner.

Two channels, same interface:
  - DashboardNotifier: the in-browser chat (jarvis/messaging.py).
  - TelegramNotifier:   casual two-way over a Telegram bot (Jarvis pings you like a colleague;
                        your replies flow back via the loop's `poll()`).

Config-gated and degrade-safe: with no Telegram token, the Telegram notifier is a no-op so
Jarvis still works (dashboard-only). Stdlib only (urllib).
"""
from __future__ import annotations
import json, os, urllib.parse, urllib.request
from abc import ABC, abstractmethod


class Notifier(ABC):
    @abstractmethod
    def tell(self, message: str) -> None: ...
    def ask(self, question: str, ref: str = "") -> None:
        """Post a question (the answer arrives asynchronously via the channel)."""
        self.tell(question)


class DashboardNotifier(Notifier):
    def tell(self, message: str) -> None:
        try:
            from jarvis import messaging
            messaging.post_note(message)
        except Exception:
            pass

    def ask(self, question: str, ref: str = "") -> None:
        try:
            from jarvis import messaging
            messaging.post_question(question, ref=ref)
        except Exception:
            pass


class TelegramNotifier(Notifier):
    """Casual two-way over the Telegram Bot API. Token/chat from env (TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID). No token => no-op."""
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, **params) -> dict:
        if not self.token:
            return {}
        url = self.API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
                return json.load(r)
        except Exception:
            return {}

    def tell(self, message: str) -> None:
        if self.enabled:
            self._call("sendMessage", chat_id=self.chat_id, text=message[:4000])

    def ask(self, question: str, ref: str = "") -> None:
        self.tell(f"❓ {question}")

    def poll(self) -> list[str]:
        """Fetch new owner replies (for the loop to ingest into the chat). Returns texts."""
        if not self.enabled:
            return []
        res = self._call("getUpdates", offset=self._offset, timeout=0)
        out = []
        for u in res.get("result", []):
            self._offset = u["update_id"] + 1
            msg = (u.get("message") or {}).get("text")
            if msg:
                out.append(msg)
        return out


def build_notifier(cfg: dict) -> Notifier:
    kind = (cfg.get("notifier", {}) or {}).get("kind", "dashboard")
    if kind == "telegram":
        tg = TelegramNotifier()
        if tg.enabled:
            return tg
    return DashboardNotifier()
