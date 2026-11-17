import json
import unittest
from daily_review import report_input


class ReportInputTests(unittest.TestCase):
    def events(self, count=10):
        return [{'id':'codex:archive:job:'+str(i),'scope_id':'session','title':'same title'*30,
                 'path':'/project','at':'2026-11-10T10:00:00+00:00',
                 'body':json.dumps({'fact':{'kind':'output','status':'pending' if i%2 else 'verified',
                    'text':'fact '+str(i)+' context '*10,'evidence':[{'id':'raw:'+str(i),'quote':'quote'*120}]},
                    'experience_candidates':[{'observation':'Observed repeatable result','fact_indexes':[i],
                                              'evidence':[{'id':'raw:'+str(i),'quote':'quote'*120}]}]})}
                for i in range(count)]

    def test_compaction_preserves_all_facts_statuses_candidates_and_references(self):
        events=self.events(1000)
        packed, refs, audit=report_input.prepare(events,lambda p:self.fail('No lossy model summary needed'))
        self.assertLess(len(json.dumps(packed,ensure_ascii=False,separators=(',',':'))),240000)
        self.assertEqual(len(packed['facts']),len(events))
        self.assertEqual(set(refs.values()),{e['id'] for e in events})
        for row,e in zip(packed['facts'],events):
            f=json.loads(e['body'])['fact']
            self.assertEqual(row[4:6],[f['status'],f['text']])
        self.assertEqual(packed['experience_candidates'][0]['text'],'Observed repeatable result')
        self.assertEqual(audit['fact_count'],1000)

    def test_oversize_batch_requires_complete_coverage(self):
        def incomplete(prompt): return json.dumps({'facts':[{'text':'missing most facts','ids':['C1']}]})
        with self.assertRaisesRegex(ValueError,'coverage'):
            report_input.prepare(self.events(100),incomplete,limit=3000)

    def test_user_attribution_and_exact_quotes_survive_compaction_and_merge_input(self):
        events=self.events(40)
        for event in events:
            body=json.loads(event['body']);body.update(evidence_roles=['user','tool'],user_evidence=['保留现有业务行为，只调整动画'])
            event['body']=json.dumps(body)
        packed,_,_=report_input.prepare(events,lambda p:self.fail('No merge needed'))
        self.assertEqual(packed['columns'][-2:],['evidence_roles','user_evidence'])
        self.assertTrue(all(row[-2:]==[['user','tool'],['保留现有业务行为，只调整动画']] for row in packed['facts']))
        def merge(prompt):
            rows=json.loads(prompt.split('INPUT_JSON')[1])['facts']
            for row in rows:
                self.assertEqual(row['user_evidence'],['保留现有业务行为，只调整动画'])
                self.assertEqual(row['evidence_roles'],['user','tool'])
            return json.dumps({'facts':[{'text':'用户限定变更边界，工具结果仅作执行依据','ids':[r['id'] for r in rows]}]})
        packed,_,audit=report_input.prepare(events,merge,limit=6000)
        self.assertTrue(audit['reductions'])
        self.assertEqual(len(packed['user_evidence']),len(events))
        self.assertTrue(all(row['quotes']==['保留现有业务行为，只调整动画'] for row in packed['user_evidence']))

    def test_oversize_batches_keep_trace_to_every_input(self):
        seen=[]
        def merge(prompt):
            data=json.loads(prompt.split('INPUT_JSON')[1]);seen.extend(data['facts'])
            return json.dumps({'facts':[{'text':'Merged observed facts, preserving pending and verified distinctions.',
                                        'ids':[r['id'] for r in data['facts']]}]})
        packed,refs,audit=report_input.prepare(self.events(100),merge,limit=12000)
        self.assertTrue(seen)
        self.assertEqual(set(refs),{i for f in packed['facts'] for i in f['ids']})
        self.assertTrue(audit['reductions'])

