"""Fresh paraphrases and scope/subject controls, evaluated with the same Flash model."""
import json
from pathlib import Path
from types import SimpleNamespace
from evaluation.stress50 import run


def questions():
    docs=json.loads((run.OUTPUT/'manifest.json').read_text())['documents']
    result=[]
    def add(kind,question,files,values,status='answered',scope=None):
        result.append(dict(id=f'h{len(result)+1:03d}',kind=kind,question=question,expected_files=files,values=values,status=status,scope=scope))
    for i in range(1,11):
        name=docs[(i-1)*3]['filename'].split('二期')[0]
        add('colloquial_subject_last',f'密钥一般隔多久要换一次？这里问的是{name}。',[docs[(i-1)*3]['filename']],[str(31+i)])
    add('multi_business_compare','青禾清算和澄海支付的生产接口最多各重试几次？',[docs[4]['filename'],docs[1]['filename']],['4','3'])
    add('environment_paraphrase','长风容量，线上能同时跑多少个？测试环境又能同时跑多少个？',[docs[38]['filename']],['600','60'])
    add('subject_middle','能查一下青禾清算吗，我想知道签名算法，还有密钥更换间隔。',[docs[3]['filename']],['HMAC-SHA256','33'])
    add('no_business_name','每批最多处理213条记录的服务叫什么，由谁审批？',[docs[30]['filename']],['山岚仓储','Owner-9001'])
    add('wrong_environment','青禾清算二期接口规范文档中，EU生产超时是多少？',[docs[4]['filename']],['554'],scope=docs[4]['filename'])
    add('missing_subject','火星清算的签名密钥每多少天更换？',[],[],'insufficient_evidence')
    add('missing_fact','青禾清算的办公大楼有几层？',[],[],'insufficient_evidence')
    add('partial_answer','青禾清算的签名算法是什么，办公大楼有几层？',[docs[3]['filename']],['HMAC-SHA256','未找到充分证据'])
    add('cross_document_paraphrase','澄海支付处理超时的消息去哪儿？那个队列堆多少条就要喊人处理，喊谁？',[docs[0]['filename'],docs[2]['filename']],['DLQ-8801','53','Duty-8801'])
    add('bare_business','青禾清算',[],[])
    return result


if __name__=='__main__':
    run.configure(SimpleNamespace(model_env=Path('/Users/cliff/Documents/TellerxChatBot/.env'),connection_doc=Path('/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md'),key_file=Path('/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt'),embedding_model='qwen3.7-text-embedding-flash',chat_model='qwen3.7-flash'))
    cases=questions()
    (run.OUTPUT/'upgrade-holdout-questions.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
    run.run_jobs(cases,lambda case:run.answer_one(case,None,True),run.OUTPUT/'upgrade-holdout-answers.jsonl',2,False)
