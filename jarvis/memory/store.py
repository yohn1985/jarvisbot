"""Episodic memory store (M3).

Postgres-backed (the `ai_memory.episodes` table) for queryable long-term memory; **degrades
to the loopback JSONL ledger** when Postgres / psycopg isn't available, so Jarvis always has
memory. Exact-key recall (`sig`) + area/recency + recurring-signature queries.

The DSN may use ${ENV} placeholders (expanded from the environment / .env).
"""
from __future__ import annotations
import json, os, re, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COLS = ["sig", "area", "source", "label", "symptom", "root_cause", "resolution", "prevent", "outcome"]


def _expand(s: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), s or "")


def _own_ledger() -> str:
    # Jarvis's OWN reflections always live here — the one place writes go on the jsonl backend.
    # Overridable via JARVIS_LEDGER (deployment knob; also lets the fitness harness round-trip
    # against a throwaway file without polluting the real ledger).
    return os.environ.get("JARVIS_LEDGER") or str(ROOT / "state" / "ledger.jsonl")


def _default_ledger() -> str | None:
    p = Path(_own_ledger())
    return str(p) if p.exists() else None


_CONN_CACHE: dict = {}


def _get_conn(dsn: str):
    """One Postgres connection per DSN per process (reused + liveness-checked). The old code opened a
    NEW connection on every build_store() — called several times per tick — and never closed them,
    exhausting the pool within hours once a DB was configured."""
    c = _CONN_CACHE.get(dsn)
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
            return c
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            _CONN_CACHE.pop(dsn, None)
    try:
        import psycopg
        c = psycopg.connect(dsn, connect_timeout=4)
        _CONN_CACHE[dsn] = c
        return c
    except Exception:
        return None


class EpisodicStore:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.conn = None
        ep = (self.cfg.get("memory", {}).get("episodic", {}) or {})
        dsn = _expand(ep.get("dsn", "")) if ep.get("kind", "postgres") == "postgres" else ""
        if dsn:
            self.conn = _get_conn(dsn)

    @property
    def backend(self) -> str:
        return "postgres" if self.conn else "jsonl"

    # --- jsonl fallback source: Jarvis's own ledger + any configured (read-only) external one ---
    def _read_paths(self) -> list[str]:
        ep = (self.cfg.get("memory", {}).get("episodic", {}) or {})
        paths = []
        if ep.get("ledger"):
            paths.append(ep["ledger"])      # optional external ledger (e.g. an audit system) — READ only
        paths.append(_own_ledger())         # Jarvis's own reflections (read + write)
        return paths

    def _jsonl(self) -> list[dict]:
        out = []
        for p in self._read_paths():
            fp = Path(p)
            if not fp.exists():
                continue
            for line in fp.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return out

    # --- writes ---
    def remember(self, **row) -> None:
        if self.conn:
            vals = [row.get(c, "") for c in COLS]
            with self.conn.cursor() as c:
                c.execute(f"INSERT INTO episodes ({','.join(COLS)}) VALUES ({','.join(['%s'] * len(COLS))})", vals)
            self.conn.commit()
            return
        # jsonl backend: PERSIST to Jarvis's own ledger (the old code silently dropped every write,
        # so reflect never reached recall). Never write the external read-only ledger.
        p = Path(_own_ledger())
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {c: row.get(c, "") for c in COLS}
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        seen = 0                            # bump recurrence so repeated experiences surface in recurring()
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("sig") and o.get("sig") == rec.get("sig"):
                    seen = max(seen, int(o.get("recurrence", 1)))
        rec["recurrence"] = seen + 1
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # --- reads ---
    def recurring(self, min_recurrence: int = 2, limit: int = 5) -> list[dict]:
        if self.conn:
            with self.conn.cursor() as c:
                c.execute("SELECT sig,area,label,symptom,recurrence FROM episodes "
                          "WHERE recurrence>=%s ORDER BY recurrence DESC LIMIT %s", (min_recurrence, limit))
                return [{"sig": r[0], "area": r[1], "label": r[2], "symptom": r[3], "recurrence": r[4]}
                        for r in c.fetchall()]
        best = {}                           # dedup appended rows by sig, keeping the highest recurrence
        for r in self._jsonl():
            sig = r.get("sig") or id(r)
            if sig not in best or int(r.get("recurrence", 1)) >= int(best[sig].get("recurrence", 1)):
                best[sig] = r
        rows = [r for r in best.values() if int(r.get("recurrence", 1)) >= min_recurrence]
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
