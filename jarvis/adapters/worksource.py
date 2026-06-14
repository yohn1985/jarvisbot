"""Work source adapter: where tasks/issues come from (folder/gitea/github)."""
from abc import ABC, abstractmethod

class WorkSource(ABC):
    @abstractmethod
    def open_items(self) -> list: ...
