"""Replay saved query plans against current retrieval; does not call answer generation."""
import argparse
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from evaluation.stress50 import run


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids')
    parser.add_argument('--report',default='upgrade-retrieval-replay.jsonl')
    args=parser.parse_args()
    run.configure(SimpleNamespace(model_env=Path('/Users/cliff/Documents/TellerxChatBot/.env'),connection_doc=Path('/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md'),key_file=Path('/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt'),embedding_model='qwen3.7-text-embedding-flash',chat_model='qwen3.7-flash'))
    from app.services.query_understanding import QueryPlan
    from app.services.retrieval import retrieval_diagnostics
    rows={}
    # Exclude degraded provider-unavailable plans; prefer successful Flash plans.
    for name in ['scenario-answers.jsonl','scenario-extra-answers.jsonl','scenario-flash-answers.jsonl','upgrade-answers.jsonl','upgrade-extra-answers.jsonl']:
        for line in (run.OUTPUT/name).read_text().splitlines():
            r=json.loads(line)
            if r.get('retrieval',{}).get('query_understanding',{}).get('model_id'):
                rows[r['id']]=r
    cases=[q for q in json.loads((run.OUTPUT/'questions.json').read_text()) if q['kind']!='global_english']+json.loads((run.OUTPUT/'scenario-extra-questions.json').read_text())
    if args.ids:cases=[q for q in cases if q['id'] in args.ids.split(',')]
    def replay(case):
        r=rows[case['id']]
        data=r['retrieval']['query_understanding']
        plan=QueryPlan(**{f.name:data[f.name] for f in dataclasses.fields(QueryPlan) if f.name in data})
        outcome=run.container().retrieval.search_with_scope(case['question'],[],query_plan=plan)
        evidence=outcome.evidence
        fulltext='\n'.join(e.content for e in evidence)
        checks={'files':set(case['expected_files']).issubset({e.filename for e in evidence}),
            'values':all(run.check_value(v,fulltext) for v in case['values']),
            'scope':not case.get('scope') or outcome.resolved_scope==case['scope'],
            'no_scope_leak':not case.get('scope') or all(e.filename==case['scope'] for e in evidence)}
        return {'id':case['id'],'kind':case['kind'],'pass':all(checks.values()),'checks':checks,'answerable_case':case['status'] in ['answered','conflict'], 'saved_plan_trace':r['response']['trace_id'],'generation_tested':False,
            'evidence':[{'filename':e.filename,'heading':e.heading_path,'content':e.content,'chunk_id':e.chunk_id} for e in evidence], 'diagnostics':retrieval_diagnostics()}
    run.run_jobs(cases,replay,run.OUTPUT/args.report,4,False)

if __name__=='__main__':main()
