"""Export submitted real-model public attempts for independent official grading."""
from __future__ import annotations
import argparse
import hashlib
import json
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delivery_agent.benchmark import export_predictions, load_manifest
from delivery_agent.repository import RepoTask, atomic_json
from scripts.grade_experiments import load_plan


def build(manifest, source, output, model_id):
    source = Path(source)
    frozen = {t['instance_id']: t for t in load_manifest(source.parent/'manifest.json')}
    metadata = json.loads(source.read_text(encoding='utf-8'))
    selection = metadata['instance_ids']
    snapshot = json.loads(source.with_name('snapshot.json').read_text(encoding='utf-8'))
    if (len(selection) != len(set(selection)) or set(selection) != set(frozen) or
            metadata['revision'] != snapshot['dataset_revision'] or metadata['dataset'] != snapshot['dataset_name']):
        raise ValueError('Official public selection differs from the frozen manifest')
    # The verifier parses bounded public artifacts and validates task/patch hashes.
    plan = load_plan(manifest)
    results = []
    seen = set()
    for item in plan:
        task, evidence, patch = item['task'], item['evidence'], item['patch']
        identity = task.instance_id if isinstance(task, RepoTask) else task['instance_id']
        if identity not in frozen:
            continue
        values = task.__dict__ if isinstance(task, RepoTask) else task
        if any(values[k] != frozen[identity][k] for k in ('repo','base_commit','problem_statement')):
            raise ValueError('Inference input differs from the frozen official public task')
        if identity in seen:
            raise ValueError('Choose exactly one real attempt per official task')
        seen.add(identity)
        results.append({'instance_id':identity,'model_name_or_path':model_id,
            'model_patch':patch.decode('utf-8') if isinstance(patch, bytes) else patch,
            'fixture':evidence['fixture'],'provider':evidence['provider'],'status':evidence['status'],
            'exit_status':evidence.get('exit_status'),'patch_valid':evidence.get('patch_valid'),
            'model_digest':evidence.get('model_digest')})
    if seen != set(selection):
        raise ValueError('Every fixed official task must have a recorded attempt, including failures')
    digests = {r['model_digest'] for r in results if r.get('model_digest')}
    if len(digests) > 1:
        raise ValueError('One official model ID cannot represent mixed model weights')
    with tempfile.TemporaryDirectory(prefix='delivery-export-') as temporary:
        paths=[]
        for result in results:
            path=Path(temporary)/(result['instance_id']+'.json')
            atomic_json(path,result); paths.append(path)
        summary=export_predictions(paths,output,model_name=model_id)
    safe={key:summary[key] for key in ('submitted_count','attempted_count','agent_failure_count','empty_patch_count')}
    safe.update(fixture=False, source_manifest_sha256=hashlib.sha256(Path(manifest).read_bytes()).hexdigest(),
        dataset_revision=json.loads(source.read_text())['revision'], model_id=model_id,
        model_digests=sorted({r['model_digest'] for r in results if r.get('model_digest')}),
        official_grade_performed=False)
    atomic_json(Path(output).with_name('official-export.json'),safe)
    return safe


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--source',default='examples/official-smoke/source.json')
    parser.add_argument('--output',required=True)
    parser.add_argument('--model-id',required=True)
    args=parser.parse_args(argv)
    print(json.dumps(build(args.manifest,args.source,args.output,args.model_id)),flush=True)


if __name__=='__main__':main()
