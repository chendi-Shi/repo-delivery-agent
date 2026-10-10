"""Independently grade saved Codex transfer trials and gate the source experience."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery_agent.repository import atomic_json, checkout, git
from delivery_agent.runtime import DockerSession
from scripts.ci_delivery_closure import collect_python_sources, load_suite_task, sha256
from scripts.closure_seed_guard import evaluate_source_seed
from scripts.grade_experiments import grade, load_plan


def source_guard(manifest, verification, output):
    runs = load_plan(manifest)
    if len(runs) != 1 or runs[0]['task'].instance_id != 'more-itertools__first-factory-v1':
        raise ValueError('source guard requires the frozen first-factory source trial')
    run = runs[0]
    source, raw = load_suite_task(ROOT / 'examples/delivery-suite-v1/more-itertools-first-factory.json')
    if run['task'] != source:
        raise ValueError('source requirement differs from frozen suite')
    result = {'schema': 1, 'kind': 'source_seed_leakage_guard',
              'manifest_sha256': sha256(Path(manifest).read_bytes()),
              'verification_sha256': sha256(Path(verification).read_bytes()),
              'source_run_id': run['id'], 'source_evidence_sha256': run['evidence_sha256'],
              'source_patch_sha256': sha256(run['patch']), 'source_task_sha256': sha256(raw),
              'eligible': False, 'functional_targets': []}
    checked = json.loads(Path(verification).read_text())
    if checked['manifest_sha256'] != result['manifest_sha256'] or len(checked['runs']) != 1:
        raise ValueError('source verification binding mismatch')
    if checked['runs'][0]['delivery_success'] is not True:
        result['reason'] = 'source_not_independently_delivered'
        atomic_json(output, result)
        return result
    with tempfile.TemporaryDirectory(prefix='codex-source-guard-') as temporary:
        workspace = checkout(source, Path(temporary) / 'workspace')
        before = collect_python_sources(workspace)
        git(workspace, 'apply', '--check', '--whitespace=nowarn', '-', input=run['patch'])
        git(workspace, 'apply', '--whitespace=nowarn', '-', input=run['patch'])
        static = evaluate_source_seed(before, collect_python_sources(workspace))
        result.update(static)
        with DockerSession(workspace, source.image, timeout=90) as environment:
            result['resolved_image'] = environment.image_id
            if environment.image_id != checked['runs'][0]['resolved_image']:
                raise ValueError('source guard and independent checks must use the same image')
            for filename in ('more-itertools-last-factory.json', 'more-itertools-split-after.json'):
                target, target_raw = load_suite_task(ROOT / 'examples/delivery-suite-v1' / filename)
                command = target.public_checks[-1]
                response = environment.execute({'command': command})
                errors = sorted(set(re.findall(r'\b(AssertionError|TypeError|AttributeError|KeyError)\b', response['output'])))
                result['functional_targets'].append({'instance_id': target.instance_id,
                    'task_sha256': sha256(target_raw),
                    'command_sha256': sha256(command), 'returncode': response['returncode'],
                    'output_sha256': sha256(response['output']), 'exception_types': errors,
                    'target_feature_still_missing': response['returncode'] != 0 and bool(errors)})
    result['eligible'] = static['eligible'] is True and all(
        row['target_feature_still_missing'] for row in result['functional_targets'])
    result['reason'] = 'verified_source_without_target_solution' if result['eligible'] else 'source_changed_or_solved_target'
    atomic_json(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', default='experiments/codex-transfer-v1/request.json')
    parser.add_argument('--output', default='runs/codex-transfer-verification')
    opts = parser.parse_args(argv)
    request = json.loads(Path(opts.request).read_text())
    manifests = request['manifests']
    if (request.get('schema') != 1 or not isinstance(manifests, list) or not 1 <= len(manifests) <= 11
            or len(set(manifests)) != len(manifests)):
        raise ValueError('invalid bounded verification request')
    output = Path(opts.output)
    output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for name in manifests:
        if not re.fullmatch(r'experiments/codex-transfer-v1/(source|targets/[a-z0-9-]+)/repository-ablation.json', name):
            raise ValueError('manifest must be inside the declared transfer cohort')
        manifest = ROOT / name
        label = manifest.parent.name
        destination = output / label
        destination.mkdir()
        verification = destination / 'verification.json'
        grade(manifest, verification, pull_images=True)
        report = json.loads(verification.read_text())
        if label == 'source':
            source_guard(manifest, verification, destination / 'source-guard.json')
        summaries.append({'label': label, 'manifest_sha256': sha256(manifest.read_bytes()),
                          'delivery_successes': report['delivery_successes'],
                          'infrastructure_failures': len(report['infrastructure_failures'])})
    atomic_json(output / 'summary.json', {'schema': 1, 'request_sha256': sha256(Path(opts.request).read_bytes()), 'trials': summaries})


if __name__ == '__main__':
    main()
