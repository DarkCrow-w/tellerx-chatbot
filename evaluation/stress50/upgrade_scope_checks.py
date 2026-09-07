"""Real PostgreSQL scope tests; all fixture mutations remain uncommitted and roll back."""
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from evaluation.stress50 import run


def main():
    run.configure(SimpleNamespace(model_env=Path('/Users/cliff/Documents/TellerxChatBot/.env'),connection_doc=Path('/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md'),key_file=Path('/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt'),embedding_model='qwen3.7-text-embedding-flash',chat_model='qwen3.7-flash'))
    from sqlalchemy import text
    from app.integrations.search import SearchIndex
    app=run.container()
    assert app.index.engine.url.database == run.FLASH_DATABASE
    checks=[]
    with app.index.engine.connect() as conn:
        transaction=conn.begin()
        class TransactionEngine:
            @contextmanager
            def connect(self):
                yield conn
        index=SearchIndex(app.settings,engine=TransactionEngine())
        try:
            docs=index.business_documents('青禾清算',[],['approved'])
            assert len(docs)==3
            document_id=docs[0]
            params={'did':document_id,'pid':str(uuid.uuid4()),'aid':str(uuid.uuid4())}
            project=conn.scalar(text('SELECT project_id FROM documents WHERE id=:did'),params)
            anchor=conn.scalar(text('SELECT c.id FROM chunks c JOIN document_versions v ON v.id=c.version_id WHERE v.document_id=:did AND v.is_current ORDER BY c.ordinal LIMIT 1'),params)
            params['anchor']=anchor
            assert index.evidence_neighbors([anchor],[project],['approved'])
            assert index.business_documents('青禾清算',['wrong-project'],['approved'])==[]
            assert index.evidence_neighbors([anchor],['wrong-project'],['approved'])==[]
            checks.append('project boundary for business discovery and neighbor expansion')
            conn.execute(text("UPDATE documents SET visibility='private' WHERE id=:did"),params)
            assert document_id not in index.business_documents('青禾清算',[],['approved'])
            assert index.evidence_neighbors([anchor],[],['approved'])==[]
            conn.execute(text("INSERT INTO principals (id,principal_type,external_id) VALUES (:pid,'user',:pid)"),params)
            conn.execute(text("INSERT INTO document_acl (id,document_id,principal_id,permission) VALUES (:aid,:did,:pid,'read')"),params)
            assert document_id in index.business_documents('青禾清算',[],['approved'],[params['pid']])
            assert index.evidence_neighbors([anchor],[],['approved'],[params['pid']])
            assert document_id not in index.business_documents('青禾清算',[],['approved'],['unauthorized-reader'])
            checks.append('private document ACL denies anonymous and wrong reader; grants explicit reader')
            conn.execute(text('UPDATE document_versions SET is_current=false WHERE document_id=:did AND is_current'),params)
            assert document_id not in index.business_documents('青禾清算',[],['approved'],[params['pid']])
            assert index.evidence_neighbors([anchor],[],['approved'],[params['pid']])==[]
            checks.append('superseded versions excluded from both new channels')
            conn.execute(text('UPDATE document_versions SET is_current=true WHERE id=(SELECT version_id FROM chunks WHERE id=:anchor)'),params)
            conn.execute(text("UPDATE chunk_search_index SET raw_text='orchidpay is the service governed by this document' WHERE chunk_id=:anchor"),params)
            assert document_id in index.business_documents('OrchidPay',[],['approved'],[params['pid']])
            assert document_id in index.business_documents('ORCHIDPAY',[],['approved'],[params['pid']])
            checks.append('English subject occurring only in body discovered case-insensitively')
            conn.execute(text('UPDATE documents SET is_deleted=true WHERE id=:did'),params)
            assert document_id not in index.business_documents('OrchidPay',[],['approved'],[params['pid']])
            assert index.evidence_neighbors([anchor],[],['approved'],[params['pid']])==[]
            checks.append('deleted documents excluded from both new channels')
        finally:
            transaction.rollback()
        assert app.index.business_documents('OrchidPay',[],['approved'])==[]
    (run.OUTPUT/'upgrade-scope-checks.json').write_text(json.dumps({'passed':len(checks),'checks':checks,'fixture_mutations':'rolled back'},ensure_ascii=False,indent=2))
    print(json.dumps({'passed':len(checks),'rolled_back':True}))

if __name__=='__main__':main()
