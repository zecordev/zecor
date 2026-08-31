-- setup/schema.sql -- the quota ledger (attempt / quota / deadend / ...)
-- applied automatically on Ledger() open; all statements are IF NOT EXISTS

CREATE TABLE IF NOT EXISTS attempt (
  id INTEGER PRIMARY KEY,
  task_id TEXT, tier TEXT, vendor TEXT, model TEXT,
  started_at TEXT, ended_at TEXT,
  in_tokens INT, out_tokens INT,
  verify_exit INT, outcome TEXT,          -- pass | fail | timeout | refused | ratelimited
  reason TEXT                             -- why route() picked this tier
);

CREATE TABLE IF NOT EXISTS quota (
  vendor TEXT, window TEXT,               -- '5h' | 'daily' | 'weekly'
  resets_at TEXT, used INT, cap INT       -- cap NULL until learned
);
CREATE UNIQUE INDEX IF NOT EXISTS quota_vendor_window ON quota (vendor, window);

CREATE TABLE IF NOT EXISTS deadend (
  fingerprint TEXT PRIMARY KEY,
  approach TEXT,
  note TEXT
);

-- Dispatch leases (Command Center). Volatile heartbeat state kept OFF the card files
-- and out of `attempt` so quota COUNT(*) math stays clean. token is the ownership key.
CREATE TABLE IF NOT EXISTS lease (
  task_id TEXT PRIMARY KEY,
  token TEXT NOT NULL,
  pid INTEGER,
  expires TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Held web deploys. When ZECOR_AUTODEPLOY is off, a merge to main queues a row here
-- instead of deploying; the console approves or rejects it.
CREATE TABLE IF NOT EXISTS pending_deploy (
  id INTEGER PRIMARY KEY,
  repo TEXT NOT NULL,
  sha TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution TEXT
);

-- Roadmap -> queue replenishment. One row per attempt to provision a Move with cards.
-- `created_ids` carries the card ids as they are made so a crash mid-op is rolled back
-- on the next run; status 'stuck' marks a Move `next_move()` should skip.
CREATE TABLE IF NOT EXISTS replenish_op (
  id INTEGER PRIMARY KEY,
  move_slug TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  status TEXT NOT NULL,                   -- running | done | stuck | rolled_back
  created_ids TEXT NOT NULL DEFAULT '[]',
  note TEXT NOT NULL DEFAULT ''
);

-- Recurring fix shapes. A signature seen RULE_PROMOTE_AT times becomes a candidate
-- rule in rules/ for a human to confirm.
CREATE TABLE IF NOT EXISTS fix_shape (
  sig TEXT PRIMARY KEY,
  count INT NOT NULL DEFAULT 0,
  first_seen TEXT, last_seen TEXT,
  examples TEXT,                          -- newline-\x1e-separated diff excerpts, cap 3
  promoted INT NOT NULL DEFAULT 0         -- 1 once a candidate rule file was written
);
