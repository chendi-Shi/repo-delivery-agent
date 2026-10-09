"""Real provider ablation; publish bounded provenance, never private trajectories.

Install .[agent]. Example: python scripts/run_experiments.py --task examples/
more-itertools-count-runs.json --output runs/live-v1 --evidence experiments
The default backend edits source only; the independent CI Docker job grades it.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from delivery_agent.cli import run_repository
from delivery_agent.repository import RepoTask, atomic_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--evidence', default='experiments')
    parser.add_argument('--model', default='ollama_chat/qwen3:4b-instruct')
    parser.add_argument('--profiles', nargs='+', choices=['baseline','recent','retrieval'], default=['baseline','recent','retrieval'])
    parser.add_argument('--max-steps', type=int, default=10)
    parser.add_argument('--context-tokens', type=int, default=12000)
    parser.add_argument('--wall-seconds', type=int, default=1200)
    parser.add_argument('--manifest', default='repository-ablation.json')
    parser.add_argument('--label', default='', help='Distinct immutable run IDs for another experimental round')
    opts = parser.parse_args(argv)
    task_value = json.loads(Path(opts.task).read_text(encoding='utf-8'))
    task = RepoTask.parse(task_value)
    root, public = Path(opts.output), Path(opts.evidence)
    manifest = public / opts.manifest
    plan = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {
        'schema': 1, 'kind': 'real_model_repository_ablation', 'runs': [],
        'limitations': ['Small exploratory sample; no general performance claim.',
            'Source-only inference has no live test execution; independent Docker grading follows.',
            'All profiles use the same task, seed, model digest and budgets; generation still need not be deterministic.']}
    for profile in opts.profiles:
        run_id = task.instance_id + '-' + profile + ('-' + opts.label if opts.label else '')
        if any(r['id'] == run_id for r in plan['runs']):
            raise ValueError('Existing evidence is immutable; choose a separate evidence manifest for reruns')
        args = SimpleNamespace(model=opts.model, profile=profile, max_steps=opts.max_steps,
            wall_seconds=opts.wall_seconds, context_tokens=opts.context_tokens, max_output_tokens=600,
            request_timeout=180, command_timeout=30, seed=42, cost_limit=3., api_base=None,
            provider='ollama_native', backend='files', ollama_mode='json', skills=[])
        directory = root / run_id
        result = run_repository(task, args, directory)
        target = public / 'model-runs' / run_id
        target.mkdir(parents=True, exist_ok=False)
        atomic_json(target / 'task.json', task_value)
        patch = result.get('model_patch', '')
        (target / 'model.patch').write_text(patch, encoding='utf-8', newline='\n')
        requests = directory / 'agent' / 'request-views.jsonl'
        logs = [json.loads(line) for line in requests.read_text(encoding='utf-8').splitlines()] if requests.exists() else []
        context = [{key: event[key] for key in ('event','call','profile','selected_source_ids','omitted_source_ids','excluded_source_ids',
            'estimated_input_tokens','estimate_method','usage','exception_type','elapsed_seconds') if key in event} for event in logs]
        evidence = {key: result.get(key) for key in ('fixture','provider','status','exit_status','seconds','error_type','patch_valid')}
        evidence.update(profile=profile, model_name=opts.model,
            model_digest=result['provenance'].get('model_digest'), task_hash=task.fingerprint(),
            patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
            usage=(result.get('delivery') or {}).get('usage') or {},
            context_requests=sum(e.get('event') == 'request' for e in logs), context_audit=context,
            verification_status='deferred', inference_backend='files', cost_source='unpriced_local',
            configuration_sha256=hashlib.sha256(json.dumps(result['provenance']['config'], sort_keys=True).encode()).hexdigest(),
            implementation_sha256=result['provenance']['implementation_sha256'],
            budgets={'max_steps': args.max_steps, 'context_tokens':args.context_tokens,'max_output_tokens':600,
                     'wall_seconds':args.wall_seconds,'request_timeout':180,'seed':42})
        atomic_json(target / 'evidence.json', evidence)
        relative = target.relative_to(public).as_posix()
        plan['runs'].append({'id':run_id,'task':relative+'/task.json','patch':relative+'/model.patch','evidence':relative+'/evidence.json'})
        public.mkdir(parents=True, exist_ok=True)
        atomic_json(manifest, plan)
        print(json.dumps({'id':run_id,'status':result['status'],'exit_status':result['exit_status'],
            'files':result['changed_files'],'usage':evidence['usage'],'seconds':result['seconds']}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__': raise SystemExit(main())
