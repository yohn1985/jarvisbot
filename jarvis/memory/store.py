"""Episodic memory store (M3).

Postgres-backed (the `ai_memory.episodes` table) for queryable long-term memory; **degrades
to the loopback JSONL ledger** when Postgres / psycopg isn't available, so Jarvis always has
memory. Exact-key recall (`sig`) + area/recency + recurring-signature queries.

The DSN may use ${ENV} placeholders (expanded from the environment / .env).
"""
from __future__ import annotations
import json, os, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COLS = ["sig", "area", "source", "label", "symptom", "root_cause", "resolution", "prevent", "outcome"]


def _expand(s: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), s or "")


def _default_ledger() -> str | None:
    # Generic default only. An env-specific external ledger goes in config.yaml as
    # memory.episodic.ledger (never hardcoded in the open-source core).
    p = ROOT / "state" / "ledger.jsonl"
    return str(p) if p.exists() else None


class EpisodicStore:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.conn = None
        ep = (self.cfg.get("memory", {}).get("episodic", {}) or {})
        dsn = _expand(ep.get("dsn", "")) if ep.get("kind", "postgres") == "postgres" else ""
        if dsn:
            try:
                import psycopg
                self.conn = psycopg.connect(dsn, connect_timeout=4)
            except Exception:
                self.conn = None

    @property
    def backend(self) -> str:
        return "postgres" if self.conn else "jsonl"

    # --- jsonl fallback source ---
    def _jsonl(self) -> list[dict]:
        ep = (self.cfg.get("memory", {}).get("episodic", {}) or {})
        p = ep.get("ledger") or _default_ledger()   # config first, then generic state/ledger.jsonl
        if not p:
            return []
        out = []
        for line in Path(p).read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    # --- writes ---
    def remember(self, **row) -> None:
        if not self.conn:
            return
        vals = [row.get(c, "") for c in COLS]
        with self.conn.cursor() as c:
            c.execute(f"INSERT INTO episodes ({','.join(COLS)}) VALUES ({','.join(['%s'] * len(COLS))})", vals)
        self.conn.commit()

    # --- reads ---
    def recurring(self, min_recurrence: int = 2, limit: int = 5) -> list[dict]:
        if self.conn:
            with self.conn.cursor() as c:
                c.execute("SELECT sig,area,label,symptom,recurrence FROM episodes "
                          "WHERE recurrence>=%s ORDER BY recurrence DESC LIMIT %s", (min_recurrence, limit))
                return [{"sig": r[0], "area": r[1], "label": r[2], "symptom": r[3], "recurrence": r[4]}
                        for r in c.fetchall()]
        rows = [r for r in self._jsonl() if int(r.get("recurrence", 1)) >= min_recurrence]
        return sorted(rows, key=lambda r: -int(r.get("recurrence", 1)))[:limit]

    def recent(self, limit: int = 5) -> list[dict]:
        if self.conn:
            with self.conn.cursor() as c:
                c.execute("SELECT sig,area,label FROM episodes ORDER BY ts DESC LIMIT %s", (limit,))
                return [{"sig": r[0], "area": r[1], "label": r[2]} for r in c.fetchall()]
        return self._jsonl()[-limit:]

    def recall(self, sig: str | None = None, area: str | None = None, limit: int = 8) -> list[dict]:
        if self.conn:
            with self.conn.cursor() as c:
                if sig:
                    c.execute("SELECT sig,area,label,symptom,prevent FROM episodes WHERE sig=%s ORDER BY ts DESC LIMIT %s", (sig, limit))
                elif area:
                    c.execute("SELECT sig,area,label,symptom,prevent FROM episodes WHERE area=%s ORDER BY ts DESC LIMIT %s", (area, limit))
                else:
                    c.execute("SELECT sig,area,label,symptom,prevent FROM episodes ORDER BY ts DESC LIMIT %s", (limit,))
                cols = [d.name for d in c.description]
                return [dict(zip(cols, r)) for r in c.fetchall()]
        rows = self._jsonl()
        if sig:
            rows = [r for r in rows if r.get("sig") == sig]
        elif area:
            rows = [r for r in rows if r.get("area") == area]
        return rows[-limit:]

    # --- migration: loopback JSONL -> postgres episodes (upsert by sig) ---
    def migrate_from_jsonl(self, path: str | None = None) -> int:
        if not self.conn:
            return -1  # no postgres; nothing to migrate into
        path = path or _default_ledger()
        if not path:
            return 0
        n = 0
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.conn.cursor() as c:
                c.execute("SELECT id,recurrence FROM episodes WHERE sig=%s", (r.get("sig"),))
                ex = c.fetchone()
                if ex:
                    c.execute("UPDATE episodes SET recurrence=%s WHERE id=%s",
                              (max(ex[1], int(r.get("recurrence", 1))), ex[0]))
                else:
                    c.execute(f"INSERT INTO episodes ({','.join(COLS)},recurrence) "
                              f"VALUES ({','.join(['%s'] * len(COLS))},%s)",
                              [r.get(k, "") for k in COLS] + [int(r.get("recurrence", 1))])
                    n += 1
            self.conn.commit()
        return n


def build_store(cfg: dict) -> EpisodicStore:
    return EpisodicStore(cfg)
