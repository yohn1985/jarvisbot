-- ai_memory: episodic + structured long-term memory (exact-key recall).
-- pgvector is added later only where semantic search is actually needed.
CREATE TABLE IF NOT EXISTS episodes (        -- the ledger: what happened
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  sig         TEXT NOT NULL,                 -- stable signature (exact-match recall)
  area        TEXT,                          -- service/module/host
  source      TEXT,                          -- fixer|deploy|prod|finder|operator
  label       TEXT,                          -- operational-friction|code-defect|...
  symptom     TEXT, root_cause TEXT, resolution TEXT, prevent TEXT,
  outcome     TEXT, recurrence INT NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS episodes_sig  ON episodes(sig);
CREATE INDEX IF NOT EXISTS episodes_area ON episodes(area, ts DESC);

CREATE TABLE IF NOT EXISTS knowledge (       -- what it understands about the env
  id BIGSERIAL PRIMARY KEY, area TEXT NOT NULL,
  fact TEXT NOT NULL, confidence TEXT, updated TIMESTAMPTZ DEFAULT now()
  -- embedding VECTOR(768)   -- add with pgvector when semantic recall is needed
);
CREATE TABLE IF NOT EXISTS questions (       -- the curiosity queue
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
  area TEXT, question TEXT NOT NULL, answered BOOLEAN DEFAULT false
);
CREATE TABLE IF NOT EXISTS watermarks (      -- per log-source last-read (bounded reads)
  source TEXT PRIMARY KEY, offset_or_ts TEXT, updated TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_map (   -- areas + staleness (curiosity driver)
  area TEXT PRIMARY KEY, last_explored TIMESTAMPTZ, understanding INT DEFAULT 0
);
