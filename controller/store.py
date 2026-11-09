"""SQLite store: aggregates, proposals and the audit trail.

Everything the controller knows lives here, so a restart is just "open the file and carry on".
One file, stdlib only. In production this maps 1:1 onto tables in the Postgres SRE already runs.
"""
import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- one row per workflow x sim-hour x caller id. hour = sim_minute // 60, negative for history
CREATE TABLE IF NOT EXISTS did_hourly (
  workflow_id TEXT, hour INTEGER, caller_id TEXT, dials INTEGER, connected INTEGER,
  PRIMARY KEY (workflow_id, hour, caller_id));

CREATE TABLE IF NOT EXISTS wf_hourly (
  workflow_id TEXT, hour INTEGER, dials INTEGER, connected INTEGER, engaged INTEGER, halluc_flags INTEGER,
  PRIMARY KEY (workflow_id, hour));

CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY, workflow_id TEXT, minute INTEGER, leads INTEGER);

-- every config version we've seen, whoever made it
CREATE TABLE IF NOT EXISTS config_changes (
  workflow_id TEXT, version INTEGER, minute INTEGER, actor TEXT, changed_dims TEXT,
  proposal_id TEXT, approval_id TEXT, PRIMARY KEY (workflow_id, version));

-- our own change requests. status: submitting -> pending -> approved -> applying -> applied
-- terminal: applied | rejected | expired | lapsed | stale | refused
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, workflow_id TEXT, kind TEXT, route TEXT, base_version INTEGER,
  status TEXT, approval_id TEXT, applied_version INTEGER, body TEXT,
  created_minute INTEGER, updated_minute INTEGER, note TEXT);

-- the audit trail: what we saw, what we decided and why
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, workflow_id TEXT, kind TEXT,
  proposal_id TEXT, summary TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions (workflow_id, minute);
"""

OPEN = ("submitting", "pending", "approved", "applying")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self._in_tx = False

    @contextmanager
    def tx(self):
        # nested calls join the outer transaction
        if self._in_tx:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self._in_tx = False

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    # ---- meta
    def get_meta(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", key)
        return json.loads(r["value"]) if r else default

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))

    # ---- audit
    def log(self, minute, workflow_id, kind, summary, detail=None, proposal_id=None):
        self.db.execute("INSERT INTO decisions (minute, workflow_id, kind, proposal_id, summary, detail) "
                        "VALUES (?,?,?,?,?,?)",
                        (minute, workflow_id, kind, proposal_id, summary, json.dumps(detail or {})))

    # ---- proposals
    def proposal(self, pid):
        return self.one("SELECT * FROM proposals WHERE proposal_id=?", pid)

    def open_proposals(self, workflow_id=None):
        sql = f"SELECT * FROM proposals WHERE status IN {OPEN}"
        if workflow_id:
            return self.q(sql + " AND workflow_id=? ORDER BY created_minute", workflow_id)
        return self.q(sql + " ORDER BY created_minute")

    def set_status(self, pid, status, minute, note=None, **cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        sql = f"UPDATE proposals SET status=?, updated_minute=?, note=COALESCE(?, note){', ' + sets if sets else ''} " \
              f"WHERE proposal_id=?"
        self.db.execute(sql, (status, minute, note, *cols.values(), pid))

    # ---- stats
    def did_window(self, workflow_id, from_hour, to_hour):
        """{caller_id: (dials, connected)} over [from_hour, to_hour)."""
        rows = self.q("SELECT caller_id, SUM(dials) d, SUM(connected) c FROM did_hourly "
                      "WHERE workflow_id=? AND hour>=? AND hour<? GROUP BY caller_id",
                      workflow_id, from_hour, to_hour)
        return {r["caller_id"]: (r["d"], r["c"]) for r in rows}

    def wf_window(self, workflow_id, from_hour, to_hour):
        r = self.one("SELECT COALESCE(SUM(dials),0) d, COALESCE(SUM(connected),0) c, COALESCE(SUM(engaged),0) e, "
                     "COALESCE(SUM(halluc_flags),0) h FROM wf_hourly WHERE workflow_id=? AND hour>=? AND hour<?",
                     workflow_id, from_hour, to_hour)
        return {"dials": r["d"], "connected": r["c"], "engaged": r["e"], "halluc_flags": r["h"]}
