"""Read-only retrieval probes for a design proposal; no answers or production edits."""
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from evaluation.stress50 import run


def main():
    run.configure(SimpleNamespace(model_env=Path('/Users/cliff/Documents/TellerxChatBot/.env'),
        connection_doc=Path('/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md'),
        key_file=Path('/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt'),
        embedding_model='qwen3.7-text-embedding-flash',chat_model='qwen3.7-flash'))
    from app.services.query_understanding import QueryPlan
    app=run.container()
    retriever=app.retrieval
    rows={}
    for file in ['scenario-answers.jsonl','scenario-flash-answers.jsonl']:
        rows.update({r['id']:r for r in map(json.loads,(run.OUTPUT/file).read_text().splitlines())})
    probes=[('q015','青禾清算','签名算法 密钥轮换周期','签名密钥轮换周期'),
            ('q132','长风容量','生产环境 测试环境 并发上限','并发上限'),
            ('q005','澄海支付','签名算法 密钥轮换周期','签名密钥轮换周期'),
            ('q006','澄海支付','同步处理超时 队列 积压 告警 值班角色','DLQ-8801')]
    originals={name:getattr(retriever,name) for name in ['_retrieve_for_statuses','_merge_fused','_deduplicate_content','_enforce_exact_identifiers','_prefer_complete_entity_matches','_select_rerank_candidate_pool']}
    active={}
    def details(results):
        hits=[]
        for rank,r in enumerate(results,1):
            source=r.get('hit',r).get('_source',{})
            if source.get('filename') in active['expected_files'] and active['needle'] in source.get('content',''):
                hits.append({'rank':rank,'filename':source.get('filename'),'heading':source.get('title_path'),'chunk_id':source.get('chunk_id')})
        return {'count':len(results),'target_hits':hits}
    def wrapped(name,method):
        def call(*args,**kw):
            result=method(*args,**kw)
            active['stages'].append({'stage':name,'query':args[0] if args and isinstance(args[0],str) else None,**details(result)})
            return result
        return call
    for name,method in originals.items():setattr(retriever,name,wrapped(name,method))
    output=[]
    for case_id,business,facts,needle in probes:
        record=rows[case_id]
        active={'id':case_id,'expected_files':record['expected_files'],'needle':needle,'stages':[]}
        plan_data=record['retrieval']['query_understanding']
        plan=QueryPlan(**{f.name:plan_data[f.name] for f in dataclasses.fields(QueryPlan) if f.name in plan_data})
        outcome=retriever.search_with_scope(record['question'],[],query_plan=plan)
        active['baseline_evidence']=[{'filename':e.filename,'heading':e.heading_path,'topic_match':needle in e.content,'target':needle in e.content and e.filename in record['expected_files']} for e in outcome.evidence]
        # Diagnostic ablation only: deleting this filter is NOT a proposed production fix.
        baseline_stages=active['stages']
        active['stages']=[]
        filter_method=retriever._prefer_complete_entity_matches
        retriever._prefer_complete_entity_matches=lambda query, rows, linked_identifiers=None: rows
        ablation=retriever.search_with_scope(record['question'],[],query_plan=plan)
        retriever._prefer_complete_entity_matches=filter_method
        active['filter_ablation_stages']=active['stages']
        active['stages']=baseline_stages
        active['filter_ablation_evidence']=[{'filename':e.filename,'heading':e.heading_path,'topic_match':needle in e.content,'target':needle in e.content and e.filename in record['expected_files']} for e in ablation.evidence]
        # The business and fact strings above are human-reviewed decompositions of the question,
        # not an automated query parser. No expected filename is passed to this search.
        candidates=app.index.document_candidates(business,[],limit=8)
        active['business_candidates']=candidates
        doc_ids=[str(c['document_id']) for c in candidates]
        fact_hits=originals['_retrieve_for_statuses'](facts,[],['approved'],document_ids=doc_ids) if doc_ids else []
        active['fact_only_in_business_documents']=details(fact_hits)
        active['prototype_note']='人工拆分问题；业务文档由名称检索发现；只验证候选召回，不代表完整问答或自动方案通过率。'
        output.append(active)
        print(case_id,'baseline',sum(x['target'] for x in active['baseline_evidence']),'scoped fact hits',active['fact_only_in_business_documents']['target_hits'],flush=True)
    (run.OUTPUT/'global-scope-probe.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
