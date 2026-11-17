import json
import tempfile
import unittest
from pathlib import Path

from daily_review import archive


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.config = {'_root': self.tmp.name}
        self.meta = {'path': '/work', 'title': 'test'}

    def output(self, ident, quote):
        return json.dumps({'facts': [{'kind': 'verification', 'text': 'verified result',
            'status': 'verified', 'evidence': [{'id': ident, 'quote': quote}]}], 'experience_candidates': []})

    def test_exact_long_citation_is_split_without_losing_any_character(self):
        body = '事实' * 303
        records = [{'id': 'session:file:123', 'kind': 'tool', 'body': body}]
        result = archive.extract(self.config, 'job', self.meta, records, lambda p:self.output('R1', body))
        refs = result['facts'][0]['evidence']
        self.assertEqual(''.join(e['quote'] for e in refs), body)
        self.assertTrue(all(e['id'] == records[0]['id'] and len(e['quote']) <= 600 for e in refs))

    def test_decoded_output_cites_original_escaped_storage(self):
        text = 'Ran 164 tests\n\nOK; "quoted"'
        body = 'Script completed\nWall time 0.3 seconds\nOutput:\n\n' + json.dumps(
            {'chunk_id': 'c', 'exit_code': 0, 'output': text})
        records = [{'id': 'session:file:123', 'kind': 'tool', 'body': body}]
        def model(prompt):
            view = json.loads(prompt.split('\nINPUT_JSON\n')[1])['records'][0]
            self.assertIn(text, view['body']); self.assertEqual(view['id'], 'R1')
            return self.output('R1', text)
        result = archive.extract(self.config, 'job', self.meta, records, model)
        self.assertIn(result['facts'][0]['evidence'][0]['quote'], body)

    def test_unsupported_quote_receives_feedback_and_can_be_repaired(self):
        records = [{'id': 's:f:1', 'kind': 'tool', 'body': 'actual'}]
        prompts = []
        def model(prompt):
            prompts.append(prompt)
            return self.output('R1', 'invented' if len(prompts) == 1 else 'actual')
        archive.extract(self.config, 'job', self.meta, records, model)
        self.assertEqual(len(prompts), 2)
        self.assertIn('facts[0].evidence[0]', prompts[1])
        self.assertIn('must match the original', prompts[1])

    def test_shortened_unknown_id_is_never_guessed_and_failure_is_audited(self):
        records = [{'id': 's:f:1', 'kind': 'tool', 'body': 'actual'}]
        calls = []
        def model(prompt): calls.append(prompt); return self.output('s:1', 'actual')
        with self.assertRaisesRegex(ValueError, 'unknown evidence'):
            archive.extract(self.config, 'job', self.meta, records, model)
        self.assertEqual(len(calls), 2)
        path = Path(self.tmp.name)/'runtime/archive-failures/job.json'
        audit = json.loads(path.read_text())
        self.assertEqual(len(audit['attempts']), 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_arbitrary_backslash_text_is_not_unescaped(self):
        body = 'literal \\n and \\u0020'
        self.assertEqual(archive.tool_body(body), body)
        records = [{'id': 's:f:1', 'kind': 'tool', 'body': body}]
        with self.assertRaises(ValueError):
            archive.extract(self.config, 'job', self.meta, records, lambda p:self.output('R1', 'not present'))


if __name__ == '__main__': unittest.main()
