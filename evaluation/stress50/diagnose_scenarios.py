"""Replay recorded query plans and inspect candidate ranks without changing production code."""
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from evaluation.stress50 import run


def main():
    run.configure(SimpleNamespace(model_env=Path('/Users/cliff/Documents/TellerxChatBot/.env'),
        connection_doc=Path('/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md'),
        key_file=Path('/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt'),
        embedding_model='qwen3.7-text-embedding-flash'))
    from app.services.query_understanding import QueryPlan
    retriever = run.container().retrieval
    rows = {r['id']:r for r in [json.loads(l) for l in (run.OUTPUT/'scenario-answers.jsonl').read_text().splitlines()]}
    flash_path=run.OUTPUT/'scenario-flash-answers.jsonl'
    if flash_path.exists():
        rows.update({r['id']:r for r in [json.loads(l) for l in flash_path.read_text().splitlines()]})
    output_path=run.OUTPUT/'scenario-retrieval-diagnostics.json'
    records=json.loads(output_path.read_text()) if output_path.exists() else []
    original = retriever._rank_candidates
    active={}
    def inspect(**kw):
        candidates=kw['candidates']
        active['calls'].append({'query':kw['query'], 'candidates':[
            {'rank':i+1,'filename':r['hit']['_source'].get('filename'),
             'heading':(r['hit']['_source'].get('heading_path') or r['hit']['_source'].get('section_path') or r['hit']['_source'].get('title_path')), 
             'expected_value_hits':[v for v in active['values'] if run.check_value(v, r['hit']['_source'].get('content',''))]}
            for i,r in enumerate(candidates)]})
        return original(**kw)
    retriever._rank_candidates=inspect
    import sys
    for case_id in (sys.argv[1:] or ['q007','q015','q074','q132']):
        if case_id not in rows: continue
        r=rows[case_id]
        p=r['retrieval']['query_understanding']
        plan=QueryPlan(**{f.name: p[f.name] for f in dataclasses.fields(QueryPlan) if f.name in p})
        active={'id':case_id,'source_trace_id':r['response']['trace_id'],'values':r['expected_values'],'calls':[]}
        outcome=retriever.search_with_scope(r['question'],[],query_plan=plan)
        active['final_evidence']=[{'filename':e.filename,'heading':e.heading_path,
            'expected_value_hits':[v for v in active['values'] if run.check_value(v,e.content)]} for e in outcome.evidence]
        records=[item for item in records if item['id']!=case_id]
        records.append(active)
        print(case_id, 'diagnosed',flush=True)
    (run.OUTPUT/'scenario-retrieval-diagnostics.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
