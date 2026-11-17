import json
import tempfile
import unittest
from pathlib import Path
from daily_review.core import Store, Policy, make_event
from daily_review import history


class SourceFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'db');self.addCleanup(self.store.close)
        self.cfg={'timezone':'Asia/Shanghai','model':{},'sources':{}}
        self.event=make_event('lark','long','2026-11-15T10:00:00+08:00','thread',
            '开头计划。'+'甲'*4500+'纠正：只是待验证方案，尚未实施。'+'乙'*4500+'结尾。',
            author={'name':'转发者'},scope_id='group')
        self.store.put(self.event)

    def test_dictionary_encoding_roundtrips_every_field_and_middle(self):
        from daily_review.source_facts import pack, unpack, fragments
        events=[self.event,{**self.event,'id':'lark:two'}]
        parts=fragments(events,2000)
        self.assertEqual(''.join(p['body'] for p in parts if p['event_id']==self.event['id']),self.event['body'])
        self.assertEqual(unpack(pack(parts)),parts)
        self.assertEqual([p['start'] for p in parts if p['event_id']==self.event['id']][0],0)
        self.assertTrue(any('纠正：' in p['body'] for p in parts))

    def test_readback_has_author_cursor_and_no_silent_tail_loss(self):
        first=history.read_evidence(self.store,self.cfg,Policy({}),self.event['id'],limit=2000)
        self.assertEqual(first['author'],self.event['author'])
        text=first['text'];page=first
        while page['next_cursor'] is not None:
            page=history.read_evidence(self.store,self.cfg,Policy({}),self.event['id'],cursor=page['next_cursor'],limit=2000)
            text+=page['text']
        self.assertEqual(text,self.event['body'])
        with self.assertRaises(ValueError):
            history.read_evidence(self.store,self.cfg,Policy({'lark_chat_ids':['group']}),self.event['id'])
        changed={**self.event,'body':'已更新'};self.store.put(changed)
        with self.assertRaises(ValueError):
            history.read_evidence(self.store,self.cfg,Policy({}),self.event['id'],cursor=first['next_cursor'])

    def test_structured_extraction_rejects_missing_disposition_and_forged_quote(self):
        from daily_review.source_facts import validate, fragments
        parts=fragments([self.event],2000)[:1]
        with self.assertRaises(ValueError):validate({'items':[]},parts)
        item={'part_id':parts[0]['part_id'],'disposition':'facts','reason':'工作',
              'facts':[{'text':'已上线','status':'completed','actor':'使用者','at':self.event['at'],
                        'kind':'result','quote':'原文中不存在的话','attribution':'confirmed'}]}
        with self.assertRaises(ValueError):validate({'items':[item]},parts)

    def test_fidelity_audit_rejects_omitted_correction_even_with_all_ids(self):
        from daily_review.source_facts import prepare
        calls=[]
        def model(prompt):
            calls.append(prompt)
            data=json.loads(prompt.split('INPUT_JSON'+chr(10),1)[1])
            if 'items' in data:
                return json.dumps({'passed':False,'reason':'遗漏中段纠正'})
            return json.dumps({'items':[{'part_id':p['part_id'],'disposition':'background',
                                'reason':'背景','facts':[]} for p in data['parts']]})
        with self.assertRaises(ValueError):
            prepare(self.store,[self.event],self.cfg,Policy({}),model,limit=1000,batch_chars=5000)
        self.assertGreaterEqual(len(calls),2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM source_fact_cache').fetchone()[0],0)

    def test_compact_history_never_silently_discards_body_or_author(self):
        item=history.compact(self.event)
        self.assertEqual(item['fact'],self.event['body'])
        self.assertEqual(item['author'],self.event['author'])

    def test_checked_extraction_cache_reused_but_model_change_invalidates(self):
        from daily_review.source_facts import prepare
        events=[{**self.event,'body':'资料。'*5000}];calls=[]
        def model(prompt):
            calls.append(prompt);data=json.loads(prompt.split('INPUT_JSON\n',1)[1])
            if 'items' in data:return json.dumps({'passed':True,'reason':'均为重复背景，无工作结论',
                           'checked_part_ids':[p['part_id'] for p in data['parts']]})
            return json.dumps({'items':[{'part_id':p['part_id'],'disposition':'background',
                'reason':'仅背景占位，无工作结论','facts':[]} for p in data['parts']]})
        value,audit=prepare(self.store,events,self.cfg,Policy({}),model,limit=15000,batch_chars=20000)
        before=len(calls)
        again,second=prepare(self.store,events,self.cfg,Policy({}),model,limit=15000,batch_chars=20000)
        self.assertEqual(value,again);self.assertEqual(len(calls),before)
        self.assertTrue(all(b['cache_hit'] for b in second['batches']))
        prepare(self.store,events,{**self.cfg,'model':{'name':'changed'}},Policy({}),model,limit=15000,batch_chars=20000)
        self.assertGreater(len(calls),before)
        before=len(calls)
        prepare(self.store,events,{**self.cfg,'identity':{'name':'changed'}},Policy({}),model,limit=15000,batch_chars=20000)
        self.assertGreater(len(calls),before)


if __name__=='__main__':unittest.main()
