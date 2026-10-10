"""Export-failure contracts with trusted fixtures; no model-performance claims."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from delivery_agent.repository import RepoTask
from scripts import run_codex_trial as runner


class CodexTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.task = RepoTask.parse({
            'instance_id': 'owner__repo-1', 'repo': 'owner/repo', 'base_commit': 'a' * 40,
            'problem_statement': 'Fix the module value.', 'image': 'python:3.11',
            'public_checks': ['python -m unittest'],
        })
        self.identity = {'kind': 'codex_cli', 'model': 'gpt-6-astra', 'cli_version': 'codex-cli fixture',
                         'binary_sha256': 'b' * 64, 'weight_digest_available': False, 'reasoning_effort': 'low'}
        self.usage = {'complete': False, 'input_tokens': 31, 'output_tokens': 7,
                      'calls_with_usage': 1, 'unknown_requests': 1}

    def run_trial(self, public, *, audit, identity=True, setup_failure=False, export_failure=None):
        private = public.parent / (public.name + '-private')

        def run(task, config, trial):
            self.assertIsNone(config.codex_binary)  # None enables the adapter's PATH discovery.
            self.assertEqual(task, self.task)
            trial.mkdir(parents=True)
            (trial / 'model.patch').write_bytes(b'')
            if audit:
                (trial / 'agent').mkdir()
                rows = [{'event': 'request', 'call': 1},
                        {'event': 'response', 'call': 1, 'usage': {'input_tokens': 31, 'output_tokens': 7}},
                        {'event': 'request', 'call': 2}, {'event': 'error', 'call': 2, 'exception_type': 'TimeoutError'}]
                (trial / 'agent/request-views.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            result = {'fixture': False, 'provider': 'codex_cli', 'status': 'failed', 'exit_status': None,
                      'seconds': 4.5, 'error_type': 'FileNotFoundError' if setup_failure else 'TimeoutError',
                      'model_patch': '', 'patch_sha256': runner.sha256(''), 'patch_valid': None,
                      'provenance': {'model_identity': self.identity if identity else None,
                                     'implementation_sha256': 'c' * 64, 'config': {}}}
            if not setup_failure:
                result['delivery'] = {'model_calls': 2, 'usage': self.usage.copy()}
            return result

        def git(_root, *args):
            return SimpleNamespace(stdout=b'a' * 40 if args == ('rev-parse', 'HEAD') else b'')

        with (patch.object(runner, 'load_suite_task', return_value=(self.task, b'frozen public task')),
              patch.object(runner, 'git', side_effect=git),
              patch.object(runner, 'run_repository', side_effect=run)):
            argv = ['--task', str(self.root / 'task.json'), '--output', str(private),
                    '--evidence', str(public), '--label', 'fixture']
            if export_failure is not None:
                with patch.object(runner, 'export_action_audit', side_effect=export_failure):
                    return runner.main(argv)
            return runner.main(argv)

    def test_export_failure_keeps_durable_measured_outcome_and_private_error_text_out(self):
        public = self.root / 'public'

        def fail_export(*args):
            # The summary must already be readable when an exporter fails.
            saved = json.loads((public / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['model_status'], 'failed')
            self.assertEqual(saved['usage'], self.usage)
            self.assertEqual(saved['model_calls'], 2)
            self.assertEqual(saved['context_requests'], 2)
            raise OSError('private response and credential diagnostic')

        with self.assertRaises(OSError):
            self.run_trial(public, audit=True, export_failure=fail_export)
        saved_text = (public / 'summary.json').read_text(encoding='utf-8')
        saved = json.loads(saved_text)
        self.assertEqual(saved['usage'], self.usage)
        self.assertEqual(saved['model_identity'], self.identity)
        self.assertEqual(saved['error_type'], 'TimeoutError')
        self.assertEqual(saved['export_error_type'], 'OSError')
        self.assertEqual(saved['attempted_count'], 1)
        self.assertFalse(saved['submitted'])
        self.assertFalse(saved['complete'])
        self.assertNotIn('private response', saved_text)

    def test_missing_audit_and_setup_failure_have_distinct_attempt_accounting(self):
        for setup_failure in (False, True):
            with self.subTest(setup_failure=setup_failure):
                public = self.root / ('setup-public' if setup_failure else 'missing-audit-public')
                with self.assertRaises(ValueError):
                    self.run_trial(public, audit=False, identity=not setup_failure, setup_failure=setup_failure)
                saved = json.loads((public / 'summary.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['model_status'], 'failed')
                self.assertEqual(saved['export_error_type'], 'ValueError')
                self.assertFalse(saved['complete'])
                if setup_failure:
                    self.assertIsNone(saved['model_identity'])
                    self.assertEqual(saved['context_requests'], 0)
                    self.assertEqual(saved['attempted_count'], 0)
                    self.assertFalse(saved['attempt_started'])
                    self.assertEqual(saved['error_type'], 'FileNotFoundError')
                else:
                    self.assertEqual(saved['model_calls'], 2)
                    self.assertEqual(saved['usage'], self.usage)
                    self.assertIsNone(saved['context_requests'])
                    self.assertIsNone(saved['attempted_count'])
                    self.assertIsNone(saved['attempt_started'])
                    self.assertEqual(saved['attempt_accounting'], 'unknown_inconsistent_request_audit')
                    self.assertFalse((public / 'model-runs').exists())


if __name__ == '__main__':
    unittest.main()
