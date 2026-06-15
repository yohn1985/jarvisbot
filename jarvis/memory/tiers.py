"""MemGPT-style tiers + self-editing API. The mind calls these during a tick.
core = pinned in-context (identity + current focus, self-editable);
recall = recent episodic (searchable); archival = cold consolidated knowledge."""
class CoreMemory:
    def append(self, block: str, text: str): ...      # core_memory_append
    def replace(self, block: str, old: str, new: str): ...  # core_memory_replace
class RecallMemory:
    def search(self, query: str, limit: int = 8) -> list: ...
class ArchivalMemory:
    def insert(self, text: str): ...
    def search(self, query: str, limit: int = 8) -> list: ...
