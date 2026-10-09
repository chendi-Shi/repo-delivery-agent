"""Real HTTP protocol and upstream integration; HTTP fixture is not model quality."""
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from delivery_agent.file_environment import FileEnvironment
from delivery_agent.ollama_model import OllamaModel
from delivery_agent.skills import SkillRegistry
from delivery_agent.toolbox import ToolboxEnvironment
from delivery_agent.knowledge import KnowledgeStore


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = FileEnvironment(self.root)
        (self.root / '.git').mkdir()
        (self.root / 'module.py').write_text('value = 1\n', encoding='utf-8')

    def test_no_generated_host_commands_or_git_escape(self):
        for command in ['python -c pass', 'echo unsafe', 'delivery-file read ../secret',
                        'delivery-file read .git/config', 'delivery-file read .GIT/config', 'delivery-file read .git./config',
                        'delivery-file write module.py:stream', 'delivery-file read CON',
                        'delivery-file read module.py; whoami']:
            with self.subTest(command=command):
                self.assertEqual(self.env.execute({'command': command})['returncode'], 2)

    def test_symlinks_cannot_escape_even_during_search(self):
        outside = self.root.parent / (self.root.name + '-external')
        outside.mkdir()
        (outside / 'secret.py').write_text('secret_unique_text', encoding='utf-8')
        self.addCleanup(lambda: ((outside / 'secret.py').unlink(), outside.rmdir()))
        try:
            (self.root / 'link.py').symlink_to(outside / 'secret.py')
        except OSError:
            return  # Windows restricted users cannot create symlinks.
        self.assertEqual(self.env.execute({'command': 'delivery-file read link.py'})['returncode'], 2)
        self.assertNotIn('secret_unique_text', self.env.execute({'command': "delivery-file search 'secret_unique_text'"})['output'])

    def test_persistent_exact_replace_and_closed_session(self):
        action = {'command': "delivery-file replace module.py <<'DELIVERY'\nvalue = 1\n---REPLACE---\nvalue = 2\nDELIVERY"}
        self.assertEqual(self.env.execute(action)['returncode'], 0)
        self.assertEqual(self.env.execute(action)['returncode'], 2)
        self.assertIn('value = 2', self.env.execute({'command': 'delivery-file read module.py'})['output'])
        self.env.close()
        with self.assertRaises(RuntimeError):
            self.env.execute({'command': 'delivery-file list'})

    def test_source_inventory_and_directory_search_find_existing_package(self):
        package = self.root/'some_package'; package.mkdir()
        (package/'__init__.py').write_text('from .core import value\n',encoding='utf-8')
        (package/'core.py').write_text('value = 1\n',encoding='utf-8')
        context = self.env.inventory('some-package', max_chars=150)
        self.assertLess(context.find('some_package/__init__.py'),context.find('module.py'))
        result=self.env.execute({'command':"delivery-file search 'value' some_package"})
        self.assertEqual(result['returncode'],0)
        self.assertIn('some_package/core.py:1',result['output'])
        self.assertNotIn('.git/',context)

    def test_toolbox_cannot_fall_through_or_retrieve_current_answer(self):
        kb = KnowledgeStore(self.root / 'kb.sqlite', 'one')
        record = kb.put(title='answer', body='current solved task', source_type='manual', source_ref='test', task_id='current',
            verified=True, verification={'kind': 'test fixture'})
        box = ToolboxEnvironment(self.env, SkillRegistry([]), kb, 'current')
        query = box.execute({'command': "delivery-knowledge search 'solved'"})
        self.assertEqual(json.loads(query['output'])['hits'], [])
        self.assertEqual(box.execute({'command': 'delivery-knowledge read ' + record['source_id']})['returncode'], 2)
        self.assertEqual(box.execute({'command': 'delivery-skill list; touch hack'})['returncode'], 2)
        self.assertFalse((self.root / 'hack').exists())


@unittest.skipUnless(importlib.util.find_spec('minisweagent'), 'pinned upstream optional dependency required')
class NativeHttpTests(unittest.TestCase):
    def test_actual_default_agent_uses_http_then_edits_file_and_submits(self):
        received = []
        commands = ["delivery-file replace module.py <<'DELIVERY'\nvalue = 1\n---REPLACE---\nvalue = 2\nDELIVERY",
                    'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT']
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.respond({'models': [{'name': 'coder:latest', 'digest': 'a'*64}]})
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.respond({'done': True, 'message': {'content': json.dumps({'command': commands[len(received)-1]})},
                              'prompt_eval_count': 50, 'eval_count': 20})
            def respond(self, value):
                body = json.dumps(value).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); workspace = root / 'workspace'; workspace.mkdir()
            (workspace / 'module.py').write_text('value = 1\n', encoding='utf-8')
            model = OllamaModel(model_name='coder', model_kwargs={'api_base': f'http://127.0.0.1:{server.server_port}'})
            from delivery_agent.upstream import run_agent
            outcome = run_agent(model, FileEnvironment(workspace), task='Fix value to 2', output_dir=root/'agent',
                agent_config={'system_template': 'Use delivery-file commands.', 'instance_template': '{{task}}', 'step_limit': 4,
                              'cost_limit': 1, 'wall_time_limit_seconds': 30}, context_tokens=16000, reserve_tokens=2000)
            self.assertEqual(outcome['exit_status'], 'Submitted')
            self.assertEqual((workspace/'module.py').read_text(), 'value = 2\n')
            self.assertEqual(outcome['delivery']['usage']['input_tokens'], 100)
            self.assertEqual(outcome['delivery']['usage']['output_tokens'], 40)
            self.assertEqual(len(received), 2)
            self.assertIn('Tool observation', received[1]['messages'][-1]['content'])


if __name__ == '__main__': unittest.main()
