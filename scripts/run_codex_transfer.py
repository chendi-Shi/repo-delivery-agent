"""Run the predeclared ten real trials in isolated processes and knowledge stores."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery_agent.repository import atomic_json
from scripts.ci_delivery_closure import load_suite_task, sha256
from scripts.run_codex_trial import load_seed_records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-binary', required=True)
    parser.add_argument('--source-db', required=True)
    parser.add_argument('--root', default='experiments/codex-transfer-v1')
    parser.add_argument('--output', default='runs/codex-transfer-v1-targets')
    opts = parser.parse_args(argv)
    evidence = Path(opts.root).resolve()
    plan_path = evidence / 'plan.json'
    plan = json.loads(plan_path.read_text())
    expected_tasks = [('more-itertools-last-factory.json', 'last', ['baseline-empty', 'retrieval-empty', 'retrieval-verified']),
                      ('more-itertools-split-after.json', 'split', ['baseline-empty', 'retrieval-empty'])]
    expected_common = {'selected_context_bytes': 64000, 'max_steps': 24, 'wall_seconds': 1800,
                       'request_timeout': 120, 'reasoning_effort': 'low'}
    if (plan['schema'] != 1 or plan['model'] != 'gpt-6-astra' or plan['provider'] != 'codex_cli'
            or plan['common'] != expected_common or plan['repetitions'] != [1, 2]
            or plan['planned_target_trials'] != 10 or len(plan['targets']) != 2):
        raise ValueError('plan differs from the predeclared bounded comparison')
    tasks = []
    for item, (filename, prefix, arms) in zip(plan['targets'], expected_tasks):
        relative = 'examples/delivery-suite-v1/' + filename
        if item != {'task': relative, 'arms': arms}:
            raise ValueError('target arms or task path changed')
        task, raw = load_suite_task(ROOT / relative)
        # Validate the actual completed seed store before spending any model calls.
        load_seed_records(Path(opts.source_db).resolve(), 'codex-transfer-v1', task.instance_id)
        tasks.append((relative, prefix, arms, sha256(raw)))
    rows = [{'label': f'{prefix}-{arm}-r{repeat}', 'task': task, 'task_sha256': digest,
             'arm': arm, 'repetition': repeat, 'profile': arm.split('-')[0]}
            for repeat in (1, 2) for task, prefix, arms, digest in tasks for arm in arms]
    private = Path(opts.output).resolve()
    target_root = evidence / 'targets'
    if private.exists() or target_root.exists() or (evidence / 'execution.json').exists():
        raise ValueError('use a new cohort; never overwrite prior attempts')
    private.mkdir(parents=True)
    target_root.mkdir()
    execution = {'schema': 1, 'fixture': False, 'plan_sha256': sha256(plan_path.read_bytes()),
                 'max_parallel_trials': 2, 'planned_order': rows, 'outcomes': [],
                 'time_scope': 'Concurrent local repository runs; latency includes shared-host and remote-service contention.'}
    atomic_json(evidence / 'execution.json', execution)

    def run(row):
        label = row['label']
        arguments = [sys.executable, str(ROOT / 'scripts/run_codex_trial.py'),
            '--task', str(ROOT / row['task']), '--model', plan['model'], '--codex-binary', opts.codex_binary,
            '--profile', row['profile'], '--max-steps', '24', '--context-tokens', '64000',
            '--wall-seconds', '1800', '--request-timeout', '120',
            '--knowledge-namespace', 'codex-transfer-v1', '--output', str(private / label),
            '--evidence', str(target_root / label), '--label', label]
        arguments += ['--knowledge-db', str(Path(opts.source_db).resolve())] if row['arm'].endswith('-verified') else ['--knowledge-empty']
        started = time.monotonic()
        with (private / (label + '.launch.log')).open('wb') as log:
            process = subprocess.Popen(arguments, stdout=log, stderr=log, cwd=ROOT, start_new_session=os.name != 'nt')
            timed_out = False
            try:
                process.wait(timeout=2100)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=log, stderr=log, timeout=15)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=15)
        return {'label': label, 'launcher_returncode': process.returncode, 'launcher_timed_out': timed_out,
                'launcher_elapsed_seconds': time.monotonic() - started,
                'summary_present': (target_root / label / 'summary.json').is_file()}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(run, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {'label': row['label'], 'launcher_error_type': type(exc).__name__}
            execution['outcomes'].append(result)
            atomic_json(evidence / 'execution.json', execution)
            print(json.dumps(result), flush=True)
    return 0 if all(row.get('launcher_returncode') == 0 for row in execution['outcomes']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
