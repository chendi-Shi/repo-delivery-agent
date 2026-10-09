"""Actual file/tool executions and pinned upstream protocol, not model quality."""
import hashlib
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from delivery_agent.file_environment import FileEnvironment
from delivery_agent.history import HistoryEnvironment, HistoryStore
from delivery_agent.knowledge import KnowledgeStore
from delivery_agent.toolbox import ToolboxEnvironment
from delivery_agent.upstream import ActionAuditEnvironment, ActionAuditError


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ActionAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.source = 'value = 1\n' + ('# source text\n' * 80)
        (self.workspace / 'module.py').write_text(self.source, encoding='utf-8')
        self.env = FileEnvironment(self.workspace)
        self.path = self.root / 'agent/action-audit.jsonl'

    def rows(self):
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]

    def test_real_read_and_rejected_write_record_full_hashes_and_bounded_previews(self):
        audit = ActionAuditEnvironment(self.env, self.path, lambda: 7)
        read = 'delivery-file read module.py 1 200'
        result = audit.execute({'command': read, 'tool_call_id': 'one'})
        self.assertEqual(result['output'], self.source)
        row = self.rows()[0]
        self.assertEqual((row['call'], row['ordinal'], row['returncode']), (7, 1, 0))
        self.assertEqual(row['command_sha256'], sha(read))
        self.assertEqual(row['output_sha256'], sha(self.source))
        self.assertEqual(row['output_preview'], self.source[:500])
        self.assertGreater(row['output_chars'], len(row['output_preview']))
        command = "delivery-file write module.py <<'DELIVERY'\n" + 'x' * 700 + '\nDELIVERY'
        rejected = audit.execute({'command': command, 'tool_call_id': 'two'})
        self.assertEqual(rejected['returncode'], 2)
        self.assertEqual((self.workspace / 'module.py').read_text(encoding='utf-8'), self.source)
        row = self.rows()[1]
        self.assertEqual(row['command_sha256'], sha(command))
        self.assertEqual(row['command_preview'], command[:500])
        self.assertEqual((row['ordinal'], row['outcome'], row['returncode']), (2, 'completed', 2))

    def test_real_closed_session_exception_is_recorded_and_rethrown(self):
        audit = ActionAuditEnvironment(self.env, self.path)
        self.env.close()
        with self.assertRaisesRegex(RuntimeError, 'Source session is closed'):
            audit.execute({'command': 'delivery-file read module.py'})
        row = self.rows()[0]
        self.assertEqual((row['outcome'], row['exception_type']), ('exception', 'RuntimeError'))
        self.assertIsNone(row['returncode'])
        self.assertIsNone(row['output_sha256'])
        self.assertNotIn('Source session is closed', self.path.read_text())

    def test_audit_limit_marks_incomplete_and_never_reexecutes_or_changes_results(self):
        with patch.object(ActionAuditEnvironment, 'MAX_FILE_BYTES', 1800):
            audit = ActionAuditEnvironment(self.env, self.path)
            for _ in range(10):
                result = audit.execute({'command': 'delivery-file read module.py 1 1'})
                self.assertEqual(result['output'], 'value = 1\n')
            self.assertLessEqual(self.path.stat().st_size, 1800)
            self.assertEqual(sum(row['event'] == 'audit_limit' for row in self.rows()), 1)
            self.assertFalse(audit.status()['complete'])
            before = self.path.read_bytes()
            reopened = ActionAuditEnvironment(self.env, self.path)
            self.assertFalse(reopened.status()['complete'])
            reopened.execute({'command': 'delivery-file read module.py 1 1'})
            self.assertEqual(self.path.read_bytes(), before)

    def test_audit_write_failure_reports_actual_execution_without_retry(self):
        audit = ActionAuditEnvironment(self.env, self.path)
        self.path.parent.rmdir()
        command = "delivery-file write new.py <<'DELIVERY'\nvalue = 2\nDELIVERY"
        with self.assertRaises(ActionAuditError):
            audit.execute({'command': command})
        self.assertEqual((self.workspace / 'new.py').read_text(), 'value = 2\n')

    def test_successful_knowledge_responses_bind_trusted_provenance(self):
        kb = KnowledgeStore(self.root / 'knowledge.sqlite', 'namespace-one')
        verification = {'kind': 'operator test fixture', 'checks': {'passed': True}}
        record = kb.put(title='generator practice', body='generator one-pass evidence',
            source_type='manual', source_ref='public-test-fixture', task_id='earlier-task',
            verified=True, verification=verification)
        toolbox = ToolboxEnvironment(self.env, knowledge=kb, task_id='current-task')
        history = HistoryStore(self.root / 'history.sqlite')
        self.addCleanup(history.close)
        audit = ActionAuditEnvironment(HistoryEnvironment(toolbox, history), self.path)
        for command in ("delivery-knowledge search 'generator'", 'delivery-knowledge read ' + record['source_id']):
            self.assertEqual(audit.execute({'command': command})['returncode'], 0)
        expected_hash = sha(json.dumps(verification, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        for row, operation in zip(self.rows(), ('search', 'read')):
            self.assertEqual(row['knowledge_operation'], operation)
            self.assertEqual(row['knowledge_source_ids'], [record['source_id']])
            self.assertEqual(row['knowledge_namespace_sha256'], kb.namespace_sha256)
            bound = row['knowledge_records'][0]
            self.assertEqual(bound['content_sha256'], record['content_sha256'])
            self.assertEqual(bound['task_id'], 'earlier-task')
            self.assertEqual(bound['verification_sha256'], expected_hash)
            self.assertTrue(bound['verified'])
            self.assertNotIn('verification', bound)

    def test_plain_environment_output_cannot_claim_trusted_knowledge_reuse(self):
        class PlainEnvironment:
            def execute(self, *args, **kwargs):
                return {'returncode': 0, 'output': json.dumps({'source_id': 'k' + 'a' * 32})}
        audit = ActionAuditEnvironment(PlainEnvironment(), self.path)
        audit.execute({'command': 'delivery-knowledge read k' + 'a' * 32})
        self.assertNotIn('knowledge_source_ids', self.rows()[0])


@unittest.skipUnless(importlib.util.find_spec('minisweagent'), 'pinned upstream optional dependency required')
class UpstreamActionAuditTests(unittest.TestCase):
    def test_real_default_agent_http_actions_and_submission_are_audited(self):
        commands = ["delivery-file replace module.py <<'DELIVERY'\nvalue = 1\n---REPLACE---\nvalue = 2\nDELIVERY",
                    'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT']
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                self.respond({'models': [{'name': 'coder:latest', 'digest': 'a' * 64}]})
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.respond({'done': True, 'message': {'content': json.dumps({'command': commands[len(received) - 1]})},
                              'prompt_eval_count': 50, 'eval_count': 20})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / 'workspace'
            work.mkdir()
            (work / 'module.py').write_text('value = 1\n')
            from delivery_agent.ollama_model import OllamaModel
            from delivery_agent.upstream import run_agent
            model = OllamaModel(model_name='coder', model_kwargs={'api_base': f'http://127.0.0.1:{server.server_port}'})
            result = run_agent(model, FileEnvironment(work), task='Protocol fixture: update the value.',
                output_dir=root / 'agent', agent_config={'system_template': 'Use delivery-file commands.',
                'instance_template': '{{task}}', 'step_limit': 4, 'cost_limit': 1.}, context_tokens=20000)
            self.assertEqual(result['exit_status'], 'Submitted')
            rows = [json.loads(line) for line in (root / 'agent/action-audit.jsonl').read_text().splitlines()]
            self.assertEqual([row['call'] for row in rows], [1, 2])
            self.assertEqual([row['ordinal'] for row in rows], [1, 1])
            self.assertEqual([row['assistant_source_id'] for row in rows], ['e000003', 'e000005'])
            self.assertEqual(rows[0]['returncode'], 0)
            self.assertEqual(rows[1]['exception_type'], 'Submitted')
            self.assertIsNone(rows[1]['returncode'])
            self.assertTrue(result['delivery']['action_audit']['complete'])
            self.assertEqual(result['delivery']['action_audit']['recorded_actions'], 2)
            self.assertIn('replace completed: module.py', received[1]['messages'][-1]['content'])


if __name__ == '__main__':
    unittest.main()
