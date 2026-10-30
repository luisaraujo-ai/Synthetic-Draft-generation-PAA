"""Durable job state (SQLite) and exactly-once cost accounting for the parallel runner."""
import json
import sqlite3

from taskgen import llm

PENDING = {'concept': 'concept_pending', 'draft': 'draft_pending', 'judge': 'judge_pending',
           'concept_novelty': 'concept_novelty_pending', 'final_novelty': 'final_novelty_pending'}
TERMINAL = {'accepted', 'rejected', 'unjudged', 'failed', 'duplicate'}
# Conservative scheduling allowances, not provider-enforced billing limits.
RESERVE_USD = {'concept':1.0, 'draft':4.0, 'judge':1.0, 'concept_novelty':1.0, 'final_novelty':1.0}


class Queue:
    """Durable job state and exactly-once accounting for returned worker receipts."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.had_budget_table = bool(self.db.execute("SELECT name FROM sqlite_master WHERE name='budget_events'").fetchone())
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS receipts (token TEXT PRIMARY KEY, records TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS budget_events (token TEXT PRIMARY KEY, amount REAL NOT NULL, status TEXT NOT NULL);
        ''')

    def put(self, ident, state, payload):
        with self.db:
            self.db.execute('INSERT INTO jobs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,payload=excluded.payload',
                            (ident, state, json.dumps(payload)))

    def rows(self):
        return [(i, s, json.loads(p)) for i, s, p in self.db.execute('SELECT * FROM jobs ORDER BY id')]

    def claim(self, ident, stage):
        with self.db:
            cursor = self.db.execute('UPDATE jobs SET state=? WHERE id=? AND state=?',
                                     (stage + '_running', ident, PENDING[stage]))
            if cursor.rowcount != 1:
                return None
            payload = self.db.execute('SELECT payload FROM jobs WHERE id=?', (ident,)).fetchone()[0]
        return json.loads(payload)

    def account(self, receipt):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)',
                            (receipt['token'], json.dumps(receipt.get('records', []))))

    def cost(self):
        records = [r for (raw,) in self.db.execute('SELECT records FROM receipts') for r in json.loads(raw)]
        # A resumed judge stage carries its original usage IDs, so cached usage is never charged twice.
        unique = {}
        for i, record in enumerate(records):
            unique[record.get('call_id', f'legacy-{i}')] = record
        return llm.cost_summary(list(unique.values()))

    def budget_usage(self):
        reserved = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM budget_events WHERE status IN ('held','uncertain')").fetchone()[0]
        return {'recorded_usd':self.cost()['total_usd'], 'reserved_usd':round(reserved,4),
                'committed_usd':round(self.cost()['total_usd']+reserved,4)}

    def reserve(self, ticket, stage, cap):
        amount = RESERVE_USD[stage]
        if cap is not None and self.budget_usage()['committed_usd']+amount>cap:
            return False
        with self.db:
            self.db.execute('INSERT INTO budget_events VALUES (?,?,?)', (ticket,amount,'held'))
        return True

    def settle(self, ticket, receipt):
        # Failed/unknown calls may still be billed; retain their allowance instead of refunding it.
        status = 'settled' if receipt['ok'] and all(r.get('usd') is not None for r in receipt.get('records',[])) else 'uncertain'
        with self.db:
            self.db.execute('UPDATE budget_events SET status=? WHERE token=?',(status,ticket))

    def retain_interrupted(self):
        with self.db:
            self.db.execute("UPDATE budget_events SET status='uncertain' WHERE status='held'")
        if not self.had_budget_table:
            # Existing runs predate reservations. Conservatively account for calls interrupted during migration.
            with self.db:
                for ident,state,data in self.rows():
                    if state.endswith('_running'):
                        stage=state[:-8]
                        self.db.execute('INSERT OR IGNORE INTO budget_events VALUES (?,?,?)',
                            (f'legacy:{ident}:{data["attempt"]}:{stage}',RESERVE_USD[stage],'uncertain'))

    def recover(self):
        for ident, state, data in self.rows():
            if state.endswith('_running'):
                self.put(ident, PENDING[state[:-8]], data)
            elif state == 'unjudged':
                target = data.get('resume_state', 'judge_pending')
                data.pop(target.removesuffix('_pending') + '_errors', None)
                self.put(ident, target, data)
