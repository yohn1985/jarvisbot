"""Memory adapter interface. Tiers wired in jarvis/memory/tiers.py."""
from abc import ABC, abstractmethod

class Memory(ABC):
    @abstractmethod
    def remember(self, **row): ...
    @abstractmethod
    def recall(self, *, sig=None, area=None, limit=8) -> list: ...
