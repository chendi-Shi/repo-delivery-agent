"""One genuine Codex-backed source-editing trial, independently graded afterwards.

Only the frozen public task and selected runtime messages reach the provider.
CLI credentials, raw responses, checkpoints and workspace stay in ignored runs.
The remote alias is recorded, but its weights cannot be pinned by this client.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery_agent.benchmark import export_predictions
from delivery_agent.cli import run_repository
from delivery_agent.knowledge import KnowledgeStore
from delivery_agent.repository import atomic_json, git
from scripts.ci_delivery_closure import (actual_request_count, export_action_audit, load_suite_task,
                                        normalized_usage, observed_source_reuse, public_preview,
                                        safe_error_type, sha256)
from scripts.grade_experiments import load_plan


ORIGIN_KEYS = ('source_id', 'content_sha256', 'task_id', 'namespace_sha256', 'verified',
               'source_ref', 'verification_sha256')
RECORD_KEYS = ('title', 'body', 'source_type', 'source_ref', 'repo', 'base_commit', 'task_id')


def canonical_hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))


def require_public_strings(value):
    # Inspect original strings: JSON escaping doubles Windows backslashes and
    # can hide a private path from a path detector applied to serialized JSON.
    if isinstance(value, str):
        if public_preview(value)[1]:
            raise ValueError('seed cannot be safely published as immutable evidence')
    elif isinstance(value, dict):
        for key, item in value.items():
            require_public_strings(key)
            require_public_strings(item)
    elif isinstance(value, list):
        for item in value:
            require_public_strings(item)


def validate_seed_verification(verification, source_ref):
    """Require the complete external-import envelope, never an internal pass."""
    hashes = ('task_sha256', 'evidence_sha256', 'patch_sha256', 'run_sha256', 'manifest_sha256',
              'verification_sha256', 'guard_sha256', 'frozen_source_task_sha256')
    if (not isinstance(verification, dict) or type(verification.get('schema')) is not int
            or verification['schema'] != 1 or verification.get('kind') != 'external_independent_public_checks'
            or verification.get('verification_backend') != 'external_docker'
            or verification.get('patch_valid') is not True or verification.get('run_status') != 'submitted'
            or verification.get('internal_verification_status') != 'deferred'
            or 'official_resolved' not in verification or verification['official_resolved'] is not None
            or not isinstance(verification.get('run_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', verification['run_id'])
            or any(not isinstance(verification.get(key), str) or
                   not re.fullmatch(r'[0-9a-f]{64}', verification[key]) for key in hashes)
            or not isinstance(verification.get('resolved_image'), str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', verification['resolved_image'])
            or not isinstance(verification.get('model_identity'), dict)):
        raise ValueError('seed requires a complete external independent verification envelope')
    identity = {key: verification[key] for key in ('run_id', 'task_sha256', 'evidence_sha256', 'patch_sha256')}
    if verification['run_sha256'] != canonical_hash(identity) or source_ref != 'run:' + verification['run_sha256']:
        raise ValueError('seed run identity is not bound to its task, evidence and patch')
    checks = verification.get('checks')
    if (not isinstance(checks, list) or not 1 <= len(checks) <= 32
            or any(not isinstance(check, dict) or not isinstance(check.get('command'), str)
                   or not 0 < len(check['command']) <= 12000 or check.get('passed') is not True
                   or type(check.get('returncode')) is not int or check['returncode'] != 0
                   or not isinstance(check.get('output_sha256'), str)
                   or not re.fullmatch(r'[0-9a-f]{64}', check['output_sha256']) for check in checks)):
        raise ValueError('seed external check receipts must be complete and passing')


def load_seed_records(path, namespace, task_id):
    """Read a completed operator-verified seed without mutating its SQLite files.

    This checks integrity, not authorship. Independent verification and leakage
    checks are performed by ingest_verified_delivery before an operator supplies
    this database. A forged operator database is outside that trust boundary.
    """
    path = Path(path)
    if (any(parent.is_symlink() for parent in (path, *path.parents))
            or not path.is_file() or not 0 < path.stat().st_size <= 8 * 1024 * 1024):
        raise ValueError('knowledge seed must be a bounded regular database')
    path = path.resolve()
    # immutable=1 avoids creating/changing a source WAL/SHM. Only a completed,
    # checkpointed seed is accepted; ignoring a live WAL would lose evidence.
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size):
            raise ValueError('knowledge seed must be closed and checkpointed')
    initial_hash = sha256(path.read_bytes())
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        rows = db.execute('SELECT * FROM knowledge WHERE namespace=? ORDER BY source_id LIMIT 9',
                          (namespace,)).fetchall()
    if sha256(path.read_bytes()) != initial_hash:
        raise ValueError('knowledge seed changed while being read')
    if not 1 <= len(rows) <= 8:
        raise ValueError('verified seed namespace must contain 1..8 records')
    records = []
    for row in rows:
        now, expiry = time.time(), row['expires_at']
        if (row['verified'] != 1 or row['status'] != 'active'
                or (expiry is not None and (type(expiry) not in (int, float)
                    or not math.isfinite(expiry) or not now < expiry <= now + 3650 * 86400))
                or row['task_id'] == task_id):
            raise ValueError('seed must be active, verified, unexpired and from another task')
        record = {key: row[key] for key in RECORD_KEYS}
        if (not all(isinstance(value, str) for value in record.values())
                or not re.fullmatch(r'run:[0-9a-f]{64}', record['source_ref'])
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', record['task_id'])
                or sha256(record['body']) != row['content_sha256']):
            raise ValueError('seed source identifiers or content hash differ')
        verification = json.loads(row['verification_json'])
        validate_seed_verification(verification, record['source_ref'])
        identity = sha256(json.dumps([record['source_type'], record['source_ref'], record['title']],
                                     separators=(',', ':')))
        if row['identity'] != identity or row['source_id'] != 'k' + sha256(sha256(namespace) + identity)[:32]:
            raise ValueError('seed identity differs from its contents or namespace')
        record.update(verified=True, verification=verification)
        # Publication must not expose a local path or a recognizable credential.
        require_public_strings(record)
        origin = {key: row[key] for key in ('source_id', 'content_sha256', 'task_id', 'source_ref')}
        origin.update(namespace_sha256=sha256(namespace), verified=True,
                      verification_sha256=canonical_hash(verification))
        records.append({'record': record, 'origin': origin, 'expires_at': expiry})
    return records, initial_hash


def prepare_knowledge(opts, task, private, public):
    """Create a new input DB per arm; later target ingestion cannot flow back."""
    enabled = opts.knowledge_empty or opts.knowledge_db is not None
    records, source_hash = (load_seed_records(opts.knowledge_db, opts.knowledge_namespace, task.instance_id)
                            if opts.knowledge_db is not None else ([], None))
    path = private / 'trial-input-knowledge.sqlite'
    if enabled:
        if path.exists():
            raise ValueError('trial knowledge database must not already exist')
        store = KnowledgeStore(path, opts.knowledge_namespace)
        for item in records:
            expiry = item['expires_at']
            imported = store.put(**item['record'], ttl_days=None if expiry is None else
                                 max(0, (expiry - time.time()) / 86400))
            imported_origin = {key: imported[key] for key in ORIGIN_KEYS if key != 'verification_sha256'}
            imported_origin['verification_sha256'] = canonical_hash(imported['verification'])
            if imported_origin != item['origin'] or imported['verified'] is not True:
                raise ValueError('trial seed differs after import')
            # Preserve the original expiry exactly, rather than extending it by
            # the small elapsed time between calculating TTL and put().
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('UPDATE knowledge SET expires_at=? WHERE source_id=?', (expiry, item['origin']['source_id']))
        store.close()
    package = {'schema': 1, 'kind': 'frozen_codex_trial_knowledge_input', 'tools_enabled': enabled,
        'mode': 'verified_seed' if records else ('empty' if enabled else 'disabled'),
        'namespace_sha256': sha256(opts.knowledge_namespace) if enabled else None,
        'source_database_sha256': source_hash, 'records': records,
        'trust_boundary': 'Operator-supplied seed from ingest_verified_delivery; hashes check integrity, not authorship.',
        'target_records_reused': False}
    atomic_json(public / 'knowledge-input.json', package)
    receipt = {'tools_enabled': enabled, 'mode': package['mode'], 'record_count': len(records),
        'namespace_sha256': package['namespace_sha256'], 'package': 'knowledge-input.json',
        'package_sha256': sha256((public / 'knowledge-input.json').read_bytes()),
        'source_database_sha256': source_hash, 'origins': [item['origin'] for item in records]}
    return str(path) if enabled else None, receipt, records


def observed_knowledge_reads(audit_path, audit, records):
    """Successful hash-bound reads of nonempty immutable source slices only."""
    matches = []
    if audit.get('available') is not True:
        return {'knowledge_observed_read': None, 'nonempty_read_count': None, 'reads': [],
                'audit_complete': False, 'interpretation': 'Read observation unavailable; usefulness is not measured.'}
    raw = audit_path.read_bytes()
    if len(raw) > 8 * 1024 * 1024 or sha256(raw) != audit.get('public_audit_sha256'):
        raise ValueError('public knowledge audit hash differs')
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
    if len(rows) != audit.get('actions'):
        raise ValueError('public knowledge audit action count differs')
    for item in records:
        origin, body = item['origin'], item['record']['body']
        if not observed_source_reuse(audit_path, origin):
            continue
        for line, row in enumerate(rows, 1):
            command = row.get('command_preview')
            if (row.get('event') != 'action' or row.get('outcome') != 'completed'
                    or type(row.get('returncode')) is not int or row['returncode'] != 0
                    or row.get('action_kind') != 'delivery-knowledge/read'
                    or row.get('knowledge_operation') != 'read'
                    or row.get('knowledge_namespace_sha256') != origin['namespace_sha256']
                    or not any(all(record.get(key) == origin[key] and type(record.get(key)) is type(origin[key])
                                   for key in ORIGIN_KEYS) for record in row.get('knowledge_records', []))
                    or not isinstance(command, str) or row.get('command_chars') != len(command)
                    or row.get('command_sha256') != sha256(command)):
                continue
            try:
                lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
                lexer.whitespace_split, lexer.commenters = True, ''
                tokens = list(lexer)
                if (not 3 <= len(tokens) <= 5 or tokens[:3] != ['delivery-knowledge', 'read', origin['source_id']]):
                    continue
                offset = int(tokens[3]) if len(tokens) >= 4 else 0
                limit = int(tokens[4]) if len(tokens) >= 5 else 2000
                if not 0 <= offset < len(body) or not 1 <= limit <= 4000:
                    continue
            except (ValueError, TypeError):
                continue
            matches.append({'audit_line': line, 'call': row.get('call'), 'ordinal': row.get('ordinal'),
                'source_id': origin['source_id'], 'content_sha256': origin['content_sha256'],
                'verification_sha256': origin['verification_sha256'], 'command_sha256': sha256(command),
                'offset': offset, 'limit': limit, 'returned_chars': len(body[offset:offset + limit]),
                'slice_sha256': sha256(body[offset:offset + limit])})
    return {'knowledge_observed_read': True if matches else (False if audit.get('complete') is True else None),
        'nonempty_read_count': len(matches) if audit.get('complete') is True else None,
        'observed_nonempty_read_count_lower_bound': len(matches), 'reads': matches,
        'audit_complete': audit.get('complete') is True, 'public_audit_sha256': sha256(raw),
        'interpretation': 'Reports actual nonempty source reads only; incorporation and benefit are not established.'}


def export_trial(result, task, opts, trial, public, run_id, summary, knowledge_records=()):
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
    reads = observed_knowledge_reads(target / 'action-audit.jsonl', audit, knowledge_records)
    reads.update(input_package_sha256=summary['knowledge_input']['package_sha256'],
                 input_package='knowledge-input.json', action_audit=(target / 'action-audit.jsonl').relative_to(public).as_posix())
    atomic_json(public / 'knowledge-observation.json', reads)
    summary['knowledge_observation'] = reads
    provenance = result['provenance']
    evidence = {key: result.get(key) for key in ('fixture', 'provider', 'status', 'exit_status',
        'seconds', 'patch_valid')}
    evidence.update(profile=opts.profile, model_name=opts.model, model_digest=None,
        model_identity=provenance.get('model_identity'), patch_sha256=sha256(patch), task_hash=task.fingerprint(),
        model_call_unit=summary['model_call_unit'], underlying_transport_request_count=None,
        usage_scope=summary['usage_scope'], error_type=summary['error_type'],
        usage=summary['usage'], model_calls=summary['model_calls'], context_requests=summary['context_requests'],
        verification_status='deferred', inference_backend='files', cost_source='chatgpt_plan_unpriced',
        monetary_cost=(result.get('delivery') or {}).get('monetary_cost'),
        action_audit=audit, implementation_sha256=provenance.get('implementation_sha256'),
        knowledge_input=summary['knowledge_input'], knowledge_observation=reads,
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
    parser.add_argument('--profile', choices=['baseline', 'recent', 'retrieval'], default='recent')
    parser.add_argument('--output', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--max-steps', type=int, default=24)
    parser.add_argument('--context-tokens', type=int, default=32000)
    parser.add_argument('--wall-seconds', type=int, default=1800)
    parser.add_argument('--request-timeout', type=int, default=120)
    knowledge = parser.add_mutually_exclusive_group()
    knowledge.add_argument('--knowledge-db', help='Completed operator-verified seed DB; copied into a new trial DB')
    knowledge.add_argument('--knowledge-empty', action='store_true', help='Enable identical knowledge tools with a new empty DB')
    parser.add_argument('--knowledge-namespace', default='repository-delivery')
    opts = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,59}', opts.label):
        parser.error('label must be a bounded artifact identifier')
    if not opts.knowledge_namespace.strip() or len(opts.knowledge_namespace) > 128:
        parser.error('knowledge namespace must contain 1..128 characters')
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
    run_id = task.instance_id + '-' + opts.profile + '-' + opts.label
    trial = private / run_id
    knowledge_db, knowledge_receipt, knowledge_records = prepare_knowledge(opts, task, private, public)
    config = SimpleNamespace(model=opts.model, codex_binary=opts.codex_binary, reasoning_effort='low',
        profile=opts.profile, max_steps=opts.max_steps, wall_seconds=opts.wall_seconds,
        context_tokens=opts.context_tokens, max_output_tokens=2048, request_timeout=opts.request_timeout,
        command_timeout=60, seed=42, cost_limit=3.0, cost_tracking=None, api_base=None,
        provider='codex_cli', backend='files', ollama_mode='json', skills=[], knowledge_db=knowledge_db,
        knowledge_namespace=opts.knowledge_namespace)
    summary = {'schema': 1, 'kind': 'real_codex_source_trial', 'fixture': False,
        'source_commit': git(ROOT, 'rev-parse', 'HEAD').stdout.decode().strip(),
        'source_tracked_tree_clean': not bool(git(ROOT, 'status', '--porcelain', '--untracked-files=no').stdout.strip()),
        'source_task_sha256': sha256(original), 'source_task_fingerprint': task.fingerprint(),
        'model_name': opts.model, 'provider': 'codex_cli', 'profile': opts.profile, 'label': opts.label,
        'model_call_unit': 'cli_invocations', 'underlying_transport_request_count': None,
        'usage_scope': 'CLI-reported completed turns; internal transport retries may have unreported usage',
        'complete': False, 'delivery_success': None, 'official_resolved': None,
        'attempt_started': None, 'attempted_count': None, 'context_requests': None,
        'usage': None, 'model_calls': None,
        'knowledge_input': knowledge_receipt, 'knowledge_observation': None,
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
        export_trial(result, task, opts, trial, public, run_id, summary, knowledge_records)
    except Exception as exc:
        summary.update(export_error_type=safe_error_type(type(exc).__name__), complete=False)
        atomic_json(public / 'summary.json', summary)
        raise
    atomic_json(public / 'summary.json', summary)
    print(json.dumps({key: summary[key] for key in ('model_status', 'exit_status', 'model_calls', 'usage', 'seconds', 'submitted')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
