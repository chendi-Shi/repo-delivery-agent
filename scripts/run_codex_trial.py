"""One genuine Codex-backed source-editing trial, independently graded afterwards.

Only the frozen public task and selected runtime messages reach the provider.
CLI credentials, raw responses, checkpoints and workspace stay in ignored runs.
The remote alias is recorded, but its weights cannot be pinned by this client.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery_agent.benchmark import export_predictions
from delivery_agent.cli import run_repository
from delivery_agent.repository import atomic_json, git
from scripts.ci_delivery_closure import (actual_request_count, export_action_audit, load_suite_task,
                                        normalized_usage, safe_error_type, sha256)
from scripts.grade_experiments import load_plan


def export_trial(result, task, opts, trial, public, run_id, summary):
    """Export only after the measured outcome is durably recorded."""
    target = public / 'model-runs' / run_id
    target.mkdir(parents=True)
    value = asdict(task)
    value['public_checks'] = list(task.public_checks)
    atomic_json(target / 'task.json', value)
    patch = result.get('model_patch', '')
    private_patch = trial / 'model.patch'
    if (not isinstance(patch, str) or len(patch.encode()) > 8 * 1024 * 1024
            or private_patch.is_symlink() or not private_patch.is_file()
            or private_patch.stat().st_size > 8 * 1024 * 1024
            or private_patch.read_bytes() != patch.encode()
            or result.get('patch_sha256') != sha256(patch)):
        raise ValueError('actual captured patch is unavailable or differs from the result')
    (target / 'model.patch').write_bytes(patch.encode())
    audit = export_action_audit(trial / 'agent/action-audit.jsonl', target / 'action-audit.jsonl')
    summary['action_audit'] = audit
    provenance = result['provenance']
    evidence = {key: result.get(key) for key in ('fixture', 'provider', 'status', 'exit_status',
        'seconds', 'patch_valid')}
    evidence.update(profile='recent', model_name=opts.model, model_digest=None,
        model_identity=provenance.get('model_identity'), patch_sha256=sha256(patch), task_hash=task.fingerprint(),
        model_call_unit=summary['model_call_unit'], underlying_transport_request_count=None,
        usage_scope=summary['usage_scope'], error_type=summary['error_type'],
        usage=summary['usage'], model_calls=summary['model_calls'], context_requests=summary['context_requests'],
        verification_status='deferred', inference_backend='files', cost_source='chatgpt_plan_unpriced',
        monetary_cost=(result.get('delivery') or {}).get('monetary_cost'),
        action_audit=audit, implementation_sha256=provenance.get('implementation_sha256'),
        configuration_sha256=sha256(json.dumps(provenance['config'], sort_keys=True)), budgets=summary['budgets'])
    atomic_json(target / 'evidence.json', evidence)
    relative = target.relative_to(public).as_posix()
    plan = {'schema': 1, 'kind': 'real_model_repository_ablation', 'runs': [{
        'id': run_id, 'task': relative + '/task.json', 'patch': relative + '/model.patch',
        'evidence': relative + '/evidence.json'}], 'limitations': [
        'One genuine remote-model attempt; not a paired comparison with previous Ollama trials.',
        'Remote model aliases can change; recorded CLI version/binary hash do not pin server weights.',
        'Source-only backend cannot execute tests; independent Docker verification remains required.',
        'CLI prompt overhead is additional to selected context; usage includes actual CLI-reported tokens.',
        'Seed and generation token cap are not enforced by this CLI adapter; request/step/wall bounds apply.',
        'Subscription inference has no known per-request dollar price; missing costs are not zero.']}
    atomic_json(public / 'repository-ablation.json', plan)
    load_plan(public / 'repository-ablation.json')
    if task.instance_id == 'psf__requests-2931':
        original_task = json.loads((ROOT / 'examples/official-smoke/psf__requests-2931.json').read_text(encoding='utf-8'))
        if any(getattr(task, key) != original_task[key] for key in ('instance_id', 'repo', 'base_commit', 'problem_statement')):
            raise ValueError('task is not the unchanged official public issue')
        exported = export_predictions([trial / 'result.json'], public / 'official-predictions.jsonl',
                                      'repo-delivery-codex-' + opts.label)
        safe = {key: exported[key] for key in ('submitted_count', 'attempted_count', 'agent_failure_count', 'empty_patch_count')}
        safe.update(official_resolved=None, original_public_task_sha256=sha256((ROOT / 'examples/official-smoke/psf__requests-2931.json').read_bytes()),
                    source_result_sha256=sha256((trial / 'result.json').read_bytes()))
        atomic_json(public / 'official-export.json', safe)
        summary['official_export'] = safe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', default='examples/delivery-suite-v1/requests-2931-public.json')
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--codex-binary', default=None)
    parser.add_argument('--output', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--max-steps', type=int, default=24)
    parser.add_argument('--context-tokens', type=int, default=32000)
    parser.add_argument('--wall-seconds', type=int, default=1800)
    parser.add_argument('--request-timeout', type=int, default=120)
    opts = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,59}', opts.label):
        parser.error('label must be a bounded artifact identifier')
    if not (1 <= opts.max_steps <= 48 and 8000 <= opts.context_tokens <= 64000
            and 60 <= opts.wall_seconds <= 3600 and 10 <= opts.request_timeout <= 600):
        parser.error('trial budgets exceed the supported bounded range')
    task_path = Path(opts.task).resolve()
    task, original = load_suite_task(task_path)
    private, public = Path(opts.output).resolve(), Path(opts.evidence).resolve()
    if private == public or private.is_relative_to(public) or public.is_relative_to(private):
        parser.error('private and public directories must be separate')
    if private.exists() or public.exists():
        parser.error('use new private and public directories for every trial')
    public.mkdir(parents=True)
    run_id = task.instance_id + '-recent-' + opts.label
    trial = private / run_id
    config = SimpleNamespace(model=opts.model, codex_binary=opts.codex_binary, reasoning_effort='low',
        profile='recent', max_steps=opts.max_steps, wall_seconds=opts.wall_seconds,
        context_tokens=opts.context_tokens, max_output_tokens=2048, request_timeout=opts.request_timeout,
        command_timeout=60, seed=42, cost_limit=3.0, cost_tracking=None, api_base=None,
        provider='codex_cli', backend='files', ollama_mode='json', skills=[], knowledge_db=None)
    summary = {'schema': 1, 'kind': 'real_codex_source_trial', 'fixture': False,
        'source_commit': git(ROOT, 'rev-parse', 'HEAD').stdout.decode().strip(),
        'source_tracked_tree_clean': not bool(git(ROOT, 'status', '--porcelain', '--untracked-files=no').stdout.strip()),
        'source_task_sha256': sha256(original), 'source_task_fingerprint': task.fingerprint(),
        'model_name': opts.model, 'provider': 'codex_cli', 'label': opts.label,
        'model_call_unit': 'cli_invocations', 'underlying_transport_request_count': None,
        'usage_scope': 'CLI-reported completed turns; internal transport retries may have unreported usage',
        'complete': False, 'delivery_success': None, 'official_resolved': None,
        'attempt_started': None, 'attempted_count': None, 'context_requests': None,
        'usage': None, 'model_calls': None,
        'scope': 'one source-only attempt; independent Docker and official grading are separate',
        'budgets': {'max_steps': opts.max_steps, 'selected_context_byte_budget': opts.context_tokens,
            'wall_seconds': opts.wall_seconds, 'request_timeout': opts.request_timeout,
            'generation_token_cap_enforced': False, 'seed_enforced': False,
            'cli_prompt_overhead_in_selected_context_budget': False,
            'output_and_protocol_reserve': 3048, 'reasoning_effort': 'low'}}
    atomic_json(public / 'summary.json', summary)
    try:
        result = run_repository(task, config, trial)
    except Exception as exc:
        summary['runner_error_type'] = safe_error_type(type(exc).__name__)
        atomic_json(public / 'summary.json', summary)
        raise
    # Save the actual outcome before reading/exporting any optional artifacts.
    # Never publish the exception message, raw model response or private config.
    summary.update(model_status=result.get('status'), exit_status=result.get('exit_status'),
        model_identity=(result.get('provenance') or {}).get('model_identity'),
        seconds=result.get('seconds'), patch_sha256=result.get('patch_sha256'), patch_valid=result.get('patch_valid'),
        error_type=safe_error_type(result.get('error_type')),
        submitted=result.get('status') == 'submitted' and result.get('exit_status') == 'Submitted')
    atomic_json(public / 'summary.json', summary)
    try:
        # Persist result measurements first; a corrupt/missing durable audit
        # must not erase known calls or confirmed partial token subtotals.
        usage, calls = normalized_usage(result)
        summary.update(usage=usage, model_calls=calls)
        atomic_json(public / 'summary.json', summary)
        usage, calls = normalized_usage(result, trial)
        summary.update(usage=usage, model_calls=calls)
        atomic_json(public / 'summary.json', summary)
        requested = actual_request_count(trial)
        if calls is not None and calls != requested:
            summary['attempt_accounting'] = 'unknown_inconsistent_request_audit'
            raise ValueError('reported provider calls differ from the persisted request audit')
        summary.update(context_requests=requested, attempt_started=requested > 0,
                       attempted_count=int(requested > 0))
        atomic_json(public / 'summary.json', summary)
        export_trial(result, task, opts, trial, public, run_id, summary)
    except Exception as exc:
        summary.update(export_error_type=safe_error_type(type(exc).__name__), complete=False)
        atomic_json(public / 'summary.json', summary)
        raise
    atomic_json(public / 'summary.json', summary)
    print(json.dumps({key: summary[key] for key in ('model_status', 'exit_status', 'model_calls', 'usage', 'seconds', 'submitted')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
