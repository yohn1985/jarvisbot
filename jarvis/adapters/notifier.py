"""Notifier adapter: how Jarvis reaches the owner (Telegram = casual, two-way)."""
from abc import ABC, abstractmethod

class Notifier(ABC):
    @abstractmethod
    def ask(self, question: str) -> str | None: ...   # casual question -> owner reply
    @abstractmethod
    def tell(self, message: str) -> None: ...
