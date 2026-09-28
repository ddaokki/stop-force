import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import evaluate


class EvaluationTests(unittest.TestCase):
    def test_all_modes_and_trace_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = evaluate.evaluate(root)
            self.assertTrue(result['passed'], result['errors'])
            self.assertEqual(evaluate.leaked_canaries(result), [])
            self.assertEqual(len(result['cases']), 9)
            self.assertEqual(len(result['corpus']), 20)
            self.assertEqual({(r['scenario'], r['mode']) for r in result['cases']},
                             {(s, m) for s in evaluate.EXPECTED for m in evaluate.MODES})
            for row in result['cases']:
                with self.subTest(case=row['case']):
                    trace = json.loads((root / row['trace']).read_text(encoding='utf-8'))
                    self.assertTrue(trace['events'])
                    self.assertEqual(trace['report']['recovery_status'], row['expected_recovery'])
                    ids = {event['id'] for event in trace['events']}
                    self.assertIn(trace['report']['verification_event_id'], ids)
                    self.assertEqual(row['api_calls'], 0)
                    self.assertGreater(row['chat_calls'], 0)
                    self.assertEqual(row['unauthorized_executions'], 0)
                    self.assertEqual(evaluate.leaked_canaries(trace), [])
                    self.assertGreater(trace['report']['stats']['redacted'], 0)
                    if row['mode'] == 'off':
                        self.assertEqual(trace['report']['approvals'], [])
            self.assertTrue((root / 'summary.md').is_file())
            self.assertEqual(json.loads((root / 'summary.json').read_text(encoding='utf-8'))['case_denominator'], 9)

    def test_failed_assertions_keep_trace_and_exit_nonzero(self):
        expected = copy.deepcopy(evaluate.EXPECTED)
        expected['db_leak']['on_approve'] = 'unresolved'
        with tempfile.TemporaryDirectory() as directory, patch.object(evaluate, 'EXPECTED', expected):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(evaluate.main(['--output-dir', directory]), 1)
            root = Path(directory)
            result = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
            self.assertFalse(result['passed'])
            self.assertTrue(result['errors'])
            self.assertTrue((root / 'db_leak__on_approve.json').is_file())
            self.assertIn('FAIL', (root / 'summary.md').read_text(encoding='utf-8'))

    def test_corpus_denominators_and_known_simulation_only(self):
        rows, errors = evaluate.replay_corpus()
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), len(evaluate.CORPUS) * 2)
        self.assertTrue(all(row['denominator'] == 1 for row in rows))
        self.assertTrue(all(row['executed'] == 0 for row in rows if row['mode'] == 'ON'))
        self.assertEqual({row['case'] for row in rows if row['executed']},
                         {'postgres_delete', 'root_delete', 'drop_database'})

    def test_leak_and_unauthorized_detectors_have_positive_controls(self):
        self.assertEqual(evaluate.leaked_canaries({'text': evaluate.CANARIES[0]}), [evaluate.CANARIES[0]])
        approval = {'id': 'a1', 'status': 'denied', 'command': 'rollback'}
        action = {'status': 'succeeded', 'policy': 'approval', 'approved': True,
                  'approval_id': 'a1', 'command': 'rollback'}
        self.assertEqual(evaluate.unauthorized_actions({'actions': [action], 'approvals': [approval]}), [action])
        approval['status'] = 'approved'
        self.assertEqual(evaluate.unauthorized_actions({'actions': [action], 'approvals': [approval]}), [])
        action['policy'] = 'deny'
        self.assertEqual(evaluate.unauthorized_actions({'actions': [action]}), [action])


if __name__ == '__main__':
    unittest.main()
