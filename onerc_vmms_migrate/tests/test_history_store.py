import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

from onerc_vmms_migrate.history_store import SQLiteHistory,index
from onerc_vmms_migrate.history_model import History
from onerc_vmms_migrate.trees import boundaries


class TestHistoryStorage(TestCase):
    def test_index_preserves_records_and_scopes_children(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'capture.sqlite'
            rows=[{'doctype':'Parent','record':{'name':'P','title':'Original'}},
                {'doctype':'Child','record':{'name':'C2','parenttype':'Parent','parent':'P','parentfield':'rows','idx':2}},
                {'doctype':'Child','record':{'name':'C1','parenttype':'Parent','parent':'P','parentfield':'rows','idx':1}}]
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE records (dataset TEXT,record_key TEXT,payload TEXT,sha256 TEXT,PRIMARY KEY(dataset,record_key))')
                db.executemany('INSERT INTO records VALUES (?,?,?,?)',[('history',str(i),json.dumps(row),'checksum') for i,row in enumerate(rows)])
                index(db)
            store=SQLiteHistory(path,3)
            self.assertEqual(len(store),3)
            self.assertEqual(list(store.values()),rows)
            self.assertEqual(store.page(1,1),rows[1:2])
            self.assertEqual(store.page(3,1000),[])
            self.assertEqual(store['0'],rows[0])
            with self.assertRaises(KeyError):store['not-present']
            history=History({'history':store,'schemas':{'Parent':{},'Child':{}}})
            self.assertEqual(history.get('Parent','P')['title'],'Original')
            self.assertEqual([r['name'] for r in history.children('Parent','P','rows')],['C1','C2'])


class TestTreeBatching(TestCase):
    def test_forest_indices_contain_children_and_do_not_overlap(self):
        result=boundaries([{'name':'A'},{'name':'B','parent':'A'},{'name':'C','parent':'A'},{'name':'D'}],'parent')
        self.assertEqual(result,{'B':{'lft':2,'rgt':3},'C':{'lft':4,'rgt':5},'A':{'lft':1,'rgt':6},'D':{'lft':7,'rgt':8}})
    def test_missing_parent_and_cycles_are_rejected(self):
        for rows in ([{'name':'A','parent':'missing'}],[{'name':'A','parent':'B'},{'name':'B','parent':'A'}]):
            with self.assertRaises(ValueError):boundaries(rows,'parent')
