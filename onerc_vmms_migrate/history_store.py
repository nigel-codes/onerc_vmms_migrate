"""Read captured history from its verified SQLite file instead of a huge JSON map."""
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path


def index(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS history_index (
        record_key TEXT PRIMARY KEY, doctype TEXT, name TEXT, parenttype TEXT,
        parent TEXT, parentfield TEXT, idx INTEGER);
        CREATE INDEX IF NOT EXISTS history_doctype ON history_index(doctype,name);
        CREATE INDEX IF NOT EXISTS history_parent ON history_index(parenttype,parent,parentfield,idx);
    ''')
    db.execute('''INSERT OR IGNORE INTO history_index
        SELECT record_key,json_extract(payload,'$.doctype'),json_extract(payload,'$.record.name'),
        json_extract(payload,'$.record.parenttype'),json_extract(payload,'$.record.parent'),
        json_extract(payload,'$.record.parentfield'),COALESCE(json_extract(payload,'$.record.idx'),0)
        FROM records WHERE dataset='history' ''')
    db.commit()


class SQLiteHistory(Mapping):
    def __init__(self,path,count):
        self.path=Path(path).resolve()
        self.count=int(count)

    def connect(self):
        return sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)

    def __len__(self):return self.count

    def __iter__(self):
        with self.connect() as db:
            for row in db.execute("SELECT record_key FROM records WHERE dataset='history' ORDER BY record_key"):
                yield row[0]

    def __getitem__(self,key):
        with self.connect() as db:
            row=db.execute("SELECT payload FROM records WHERE dataset='history' AND record_key=?",(key,)).fetchone()
        if row is None:raise KeyError(key)
        return json.loads(row[0])

    def values(self):
        with self.connect() as db:
            for row in db.execute("SELECT payload FROM records WHERE dataset='history' ORDER BY record_key"):
                yield json.loads(row[0])

    def rows(self,doctype):
        with self.connect() as db:
            for row in db.execute("SELECT r.payload FROM history_index i JOIN records r ON r.record_key=i.record_key AND r.dataset='history' WHERE i.doctype=? ORDER BY i.name",(doctype,)):
                yield json.loads(row[0])['record']

    def children(self,doctype,name,field):
        with self.connect() as db:
            return [json.loads(row[0])['record'] for row in db.execute("SELECT r.payload FROM history_index i JOIN records r ON r.record_key=i.record_key AND r.dataset='history' WHERE i.parenttype=? AND i.parent=? AND i.parentfield=? ORDER BY i.idx,i.name",(doctype,name,field))]


class Tables(Mapping):
    def __init__(self,store,schemas):
        self.store,self.schemas,self.cache=store,schemas,{}
    def __iter__(self):return iter(self.schemas)
    def __len__(self):return len(self.schemas)
    def __getitem__(self,doctype):
        if doctype not in self.cache:
            self.cache[doctype]={r['name']:r for r in self.store.rows(doctype)}
        return self.cache[doctype]
