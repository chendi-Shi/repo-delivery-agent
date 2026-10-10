"""Export-failure contracts with trusted fixtures; no model-performance claims."""
from __future__ import annotations

import json
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from delivery_agent.repository import RepoTask
from delivery_agent.knowledge import KnowledgeStore
from delivery_agent.toolbox import ToolboxEnvironment
from delivery_agent.upstream import ActionAuditEnvironment
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

    def run_trial(self, public, *, audit, identity=True, setup_failure=False, export_failure=None, profile=None,
                  extra_args=(), inspect_config=None):
        private = public.parent / (public.name + '-private')

        def run(task, config, trial):
            self.assertIsNone(config.codex_binary)  # None enables the adapter's PATH discovery.
            self.assertEqual(config.profile, profile or 'recent')
            self.assertEqual(trial.name, self.task.instance_id + '-' + (profile or 'recent') + '-fixture')
            self.assertEqual(task, self.task)
            if inspect_config is not None:
                inspect_config(config)
            else:
                self.assertIsNone(config.knowledge_db)  # Historical default stays disabled.
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
            if profile is not None:
                argv += ['--profile', profile]
            argv += list(extra_args)
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

    def test_selected_profile_reaches_runtime_summary_and_exported_evidence(self):
        for profile in ('baseline', 'retrieval'):
            with self.subTest(profile=profile):
                public = self.root / (profile + '-public')
                self.assertEqual(self.run_trial(public, audit=True, profile=profile), 0)
                summary = json.loads((public / 'summary.json').read_text(encoding='utf-8'))
                self.assertEqual(summary['profile'], profile)
                manifest = json.loads((public / 'repository-ablation.json').read_text(encoding='utf-8'))
                run = manifest['runs'][0]
                self.assertEqual(run['id'], self.task.instance_id + '-' + profile + '-fixture')
                evidence = json.loads((public / run['evidence']).read_text(encoding='utf-8'))
                self.assertEqual(evidence['profile'], profile)

    def test_explicit_empty_knowledge_reaches_runtime_and_public_receipt(self):
        paths = []

        def inspect(config):
            paths.append(Path(config.knowledge_db))
            self.assertEqual(config.knowledge_namespace, 'fixture-namespace')
            store = KnowledgeStore(config.knowledge_db, config.knowledge_namespace)
            self.assertEqual(store.stats()['total'], 0)

        for profile in ('baseline', 'retrieval'):
            public = self.root / ('empty-' + profile)
            self.assertEqual(self.run_trial(public, audit=True, profile=profile,
                extra_args=['--knowledge-empty', '--knowledge-namespace', 'fixture-namespace'],
                inspect_config=inspect), 0)
            summary = json.loads((public / 'summary.json').read_text(encoding='utf-8'))
            self.assertTrue(summary['knowledge_input']['tools_enabled'])
            self.assertEqual(summary['knowledge_input']['mode'], 'empty')
            self.assertEqual(summary['knowledge_input']['record_count'], 0)
            self.assertNotIn(str(paths[-1]), (public / 'summary.json').read_text(encoding='utf-8'))
        self.assertNotEqual(paths[0], paths[1])


class CodexKnowledgeTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.namespace = 'test-codex-transfer'
        self.target = SimpleNamespace(instance_id='target-task')
        identity = {'run_id': 'source-baseline-fixture', 'task_sha256': 'a' * 64,
                    'evidence_sha256': 'b' * 64, 'patch_sha256': 'c' * 64}
        self.verification = {'schema': 1, 'kind': 'external_independent_public_checks',
            'verification_backend': 'external_docker', **identity, 'run_sha256': runner.canonical_hash(identity),
            'manifest_sha256': 'd' * 64, 'verification_sha256': 'e' * 64, 'guard_sha256': 'f' * 64,
            'frozen_source_task_sha256': '1' * 64, 'patch_valid': True, 'run_status': 'submitted',
            'internal_verification_status': 'deferred', 'resolved_image': 'sha256:' + '2' * 64,
            'model_identity': {'kind': 'codex_cli'},
            'checks': [{'command': 'python -m unittest', 'passed': True, 'returncode': 0, 'output_sha256': '3' * 64}],
            'official_resolved': None, 'trust_boundary': 'trusted operator fixture'}
        self.record = {'title': 'source delivery', 'body': 'Public task and actual source evidence.\nPatch excerpt.',
            'source_type': 'repository_run', 'source_ref': 'run:' + self.verification['run_sha256'],
            'repo': 'owner/repo', 'base_commit': 'a' * 40, 'task_id': 'source-task', 'verified': True,
            'verification': self.verification}

    def seed(self, name='seed.sqlite', *, record=None, ttl_days=None):
        path = self.root / name
        store = KnowledgeStore(path, self.namespace)
        metadata = store.put(**(record or self.record), ttl_days=ttl_days)
        store.close()
        return path, metadata

    def prepare(self, source, label):
        public = self.root / (label + '-public')
        public.mkdir()
        private = self.root / (label + '-private')
        opts = SimpleNamespace(knowledge_empty=source is None, knowledge_db=source,
                               knowledge_namespace=self.namespace)
        return runner.prepare_knowledge(opts, self.target, private, public), public

    def test_completed_seed_copied_without_mutation_and_target_data_cannot_flow_back(self):
        source, metadata = self.seed(ttl_days=2)
        original = source.read_bytes()
        first, public = self.prepare(source, 'first')
        first_path, receipt, frozen = first
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(receipt['mode'], 'verified_seed')
        self.assertEqual(receipt['record_count'], 1)
        self.assertEqual(frozen[0]['expires_at'], metadata['expires_at'])
        first_store = KnowledgeStore(first_path, self.namespace)
        self.assertEqual(first_store.read(metadata['source_id'])['expires_at'], metadata['expires_at'])
        first_store.put(title='Target response', body='must never flow to next target', source_type='repository_run',
                        source_ref='run:' + '9' * 64, task_id='target-task')
        second, _ = self.prepare(source, 'second')
        self.assertNotEqual(first_path, second[0])
        self.assertEqual(KnowledgeStore(second[0], self.namespace).stats()['total'], 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(Path(str(source) + '-wal').exists())
        package = json.loads((public / 'knowledge-input.json').read_text(encoding='utf-8'))
        self.assertEqual(package['records'][0]['record']['body'], self.record['body'])
        self.assertEqual(receipt['package_sha256'], runner.sha256((public / 'knowledge-input.json').read_bytes()))
        self.assertEqual(frozen[0]['origin']['verification_sha256'], runner.canonical_hash(self.verification))
        self.assertNotIn(str(source), (public / 'knowledge-input.json').read_text(encoding='utf-8'))

    def test_bad_seed_provenance_and_current_task_fail_closed(self):
        for index, change in enumerate(({'verified': False}, {'task_id': self.target.instance_id},
                {'verification': {'kind': 'external_independent_public_checks'}},
                {'verification': {**self.verification, 'internal_verification_status': 'passed'}},
                {'verification': {**self.verification, 'guard_sha256': None}},
                {'verification': {**self.verification, 'checks': []}},
                {'verification': {**self.verification, 'patch_sha256': '8' * 64}},
                {'source_ref': 'run:' + '9' * 64}, {'body': 'ghp_' + 'a' * 36},
                {'body': 'C:\\Users\\PrivatePerson\\evidence.txt'},
                {'verification': {**self.verification, 'trust_boundary': 'C:\\Users\\PrivatePerson\\evidence.txt'}})):
            with self.subTest(change=change):
                source, _ = self.seed('bad-' + str(index) + '.sqlite', record={**self.record, **change})
                with self.assertRaises(ValueError):
                    runner.load_seed_records(source, self.namespace, self.target.instance_id)
        source, _ = self.seed('expired.sqlite', ttl_days=0)
        with self.assertRaises(ValueError):
            runner.load_seed_records(source, self.namespace, self.target.instance_id)

    def test_hash_corruption_stale_database_and_live_wal_are_rejected(self):
        for index, sql in enumerate(("UPDATE knowledge SET body='corrupt'", "UPDATE knowledge SET status='stale'",
                                     "UPDATE knowledge SET source_id='k00000000000000000000000000000000'")):
            source, _ = self.seed('corrupt-' + str(index) + '.sqlite')
            db = sqlite3.connect(source)
            with db:
                db.execute(sql)
            db.close()
            with self.assertRaises(ValueError):
                runner.load_seed_records(source, self.namespace, self.target.instance_id)
        source, _ = self.seed('live.sqlite')
        Path(str(source) + '-wal').write_bytes(b'uncheckpointed')
        with self.assertRaisesRegex(ValueError, 'checkpointed'):
            runner.load_seed_records(source, self.namespace, self.target.instance_id)

    def actual_audit(self, source, commands):
        prepared, public = self.prepare(source, 'audit')
        private_db, _, frozen = prepared
        store = KnowledgeStore(private_db, self.namespace)
        # Actual KnowledgeStore -> ToolboxEnvironment -> ActionAuditEnvironment
        # is exercised; shell text cannot mint knowledge provenance metadata.
        env = ToolboxEnvironment(SimpleNamespace(execute=lambda *_args, **_kwargs:
            {'output': 'shell text', 'returncode': 0, 'exception_info': ''}), knowledge=store,
            task_id=self.target.instance_id)
        raw_path, exported = self.root / 'raw-actions.jsonl', public / 'action-audit.jsonl'
        wrapper = ActionAuditEnvironment(env, raw_path, call_number=lambda: 1)
        for command in commands:
            wrapper.execute({'command': command})
        audit = runner.export_action_audit(raw_path, exported)
        return exported, audit, frozen

    def test_actual_search_and_empty_read_are_not_nonempty_reads(self):
        source, metadata = self.seed()
        source_id = metadata['source_id']
        path, audit, frozen = self.actual_audit(source, [
            "delivery-knowledge search 'source' 5", f'delivery-knowledge read {source_id} 9999 10',
            f'delivery-knowledge read {source_id} -1 5', f'delivery-knowledge read {source_id} 2 3'])
        observed = runner.observed_knowledge_reads(path, audit, frozen)
        self.assertTrue(observed['knowledge_observed_read'])
        self.assertEqual(observed['nonempty_read_count'], 1)
        self.assertEqual(observed['reads'][0]['audit_line'], 4)
        self.assertEqual(observed['reads'][0]['returned_chars'], 3)
        self.assertEqual(observed['reads'][0]['slice_sha256'], runner.sha256(self.record['body'][2:5]))
        self.assertEqual(audit['kind_counts'], {'delivery-knowledge/search': 1, 'delivery-knowledge/read': 3})

    def test_metadata_only_truncated_failed_and_wrong_origin_reads_do_not_qualify(self):
        source, metadata = self.seed()
        path, audit, frozen = self.actual_audit(source, ['delivery-knowledge read ' + metadata['source_id']])
        row = json.loads(path.read_text(encoding='utf-8').strip())
        variants = []
        for change in ({'command_preview': row['command_preview'][:-1]}, {'command_sha256': '0' * 64},
                {'returncode': 2}, {'returncode': False}, {'outcome': 'error'},
                {'action_kind': 'delivery-knowledge/search'}, {'knowledge_operation': 'search'},
                {'knowledge_namespace_sha256': '0' * 64}):
            variants.append({**row, **change})
        for command in ('delivery-knowledge read ' + metadata['source_id'] + ' 9999 5',
                        'delivery-knowledge read ' + metadata['source_id'] + ' 0 0',
                        'delivery-knowledge read ' + metadata['source_id'] + '; echo spoof'):
            variants.append({**row, 'command_preview': command, 'command_chars': len(command),
                             'command_sha256': runner.sha256(command)})
        bad_origin = copy.deepcopy(row)
        bad_origin['knowledge_records'][0]['verification_sha256'] = '0' * 64
        variants.append(bad_origin)
        for variant in variants:
            with self.subTest(variant=variant):
                path.write_text(json.dumps(variant) + '\n', encoding='utf-8')
                varied_audit = {**audit, 'public_audit_sha256': runner.sha256(path.read_bytes())}
                self.assertFalse(runner.observed_knowledge_reads(path, varied_audit, frozen)['knowledge_observed_read'])

    def test_incomplete_audit_does_not_turn_missing_reads_into_false_or_zero(self):
        source, metadata = self.seed()
        path, audit, frozen = self.actual_audit(source, ['delivery-knowledge read ' + metadata['source_id']])
        incomplete = runner.observed_knowledge_reads(path, {**audit, 'complete': False}, frozen)
        self.assertTrue(incomplete['knowledge_observed_read'])
        self.assertIsNone(incomplete['nonempty_read_count'])
        self.assertEqual(incomplete['observed_nonempty_read_count_lower_bound'], 1)
        absent = runner.observed_knowledge_reads(path, {**audit, 'complete': False}, [])
        self.assertIsNone(absent['knowledge_observed_read'])
        self.assertIsNone(absent['nonempty_read_count'])
        missing = runner.observed_knowledge_reads(path, {'available': False}, frozen)
        self.assertIsNone(missing['knowledge_observed_read'])
        with self.assertRaises(ValueError):
            runner.observed_knowledge_reads(path, {**audit, 'public_audit_sha256': '0' * 64}, frozen)


if __name__ == '__main__':
    unittest.main()
