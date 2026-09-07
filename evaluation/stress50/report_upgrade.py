"""Report staged-model post-upgrade regression, separate from held-out questions."""
import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path
from evaluation.stress50.run import OUTPUT, ROOT
from evaluation.stress50.report_scenarios import LABELS


def read(name):
    p=OUTPUT/name
    return [json.loads(line) for line in p.read_text().splitlines() if line] if p.exists() else []


def main():
    cases=[q for q in json.loads((OUTPUT/'questions.json').read_text()) if q['kind']!='global_english']+json.loads((OUTPUT/'scenario-extra-questions.json').read_text())
    dated=read('upgrade-dated-answers.jsonl')
    current=read('upgrade-answers.jsonl')+read('upgrade-extra-answers.jsonl')+dated
    byid={r['id']:r for r in current}
    current=list(byid.values())
    held=read('upgrade-holdout-answers.jsonl')
    baseline={r['id']:r for f in ['scenario-answers.jsonl','scenario-extra-answers.jsonl','scenario-flash-answers.jsonl'] for r in read(f)}
    errors=[r['id'] for r in current+held if r.get('error') or r.get('retrieval',{}).get('query_understanding',{}).get('fallback_reason')=='NoModelAvailable' or '生成模型当前不可用' in r.get('response',{}).get('answer','')]
    valid = [r for r in current if r['id'] not in errors]
    complete=len(byid)==len(cases) and len(held)==20 and not errors and all(r.get('pass') for r in current+held)
    total_pass=sum(r.get('pass',False) for r in valid)
    held_pass=sum(r.get('pass',False) for r in held)
    groups=defaultdict(list)
    for q in cases:groups[q['kind']].append(q['id'])
    lines=['# 全库业务关键词检索升级与实测', '', '日期：2026-09-07。', '',
       f'分阶段回归：计划并尝试{len(cases)}题，有效完成{len(valid)}题，通过{total_pass}题，环境阻塞{len(errors)}题；新增留出题：完成{len(held)}/20，通过{held_pass}题。验收状态：{"完成" if complete else "尚未完成（见未通过或受阻记录）"}。', '',
       '## 实施内容', '',
       '1. 业务主体与所问事实分开解析；已落地的主体可以关联整份文档的章节，不再要求正文包含整段问句。',
       '2. 按文件名或正文发现业务相关文档组，带原有项目、ACL、当前版本条件逐问题项召回。原问题、查询计划及业务事实通道在候选层合并，再共用主体过滤和最终证据预算。',
       '3. 优先覆盖不同问题项；关联编号只从与问题事实相关的候选发现；围绕命中块读取同章节及相邻块，采用每个命中块独立预算。',
       '4. 部分问题有证据时保留有引用的答案，并列出未找到证据的问题项；缺项标签限定为查询计划中的问题项。',
       '5. QueryTrace新增candidate_stages，保存文档组、事实查询、过滤前后及最终候选排名；支持BUSINESS_RETRIEVAL_ENABLED关闭新通道进行排查。', '',
       '## 测试配置与口径', '',
       '首轮问答和查询理解使用qwen3.7-flash；按用户要求，55道额度阻塞题及q086改用qwen3.7-flash-2026-07-15补测，逐题采用最新结果。新增20道留出题保持原Flash结果。本报告属于两阶段模型版本结果，不是单一模型的150题通过率。Embedding为qwen3.7-text-embedding-flash/1024维，Rerank关闭；同一50份原文件、同一已入库索引。调用与/chat相同的应用服务，空project_ids表示全部可访问知识库。标准题135道＋原内容线索题15道，共150道，排除10道英文别名题；另加20道未用于原方案诊断的问法。', '',
       '历史基线为Max与Flash两阶段结果，仅用于定位已知问题；历史143/150不应与本轮相减后归因成纯模型或纯算法收益。本轮补测包含模型版本切换，因此不把总通过率差异全部归因于代码升级。留出20题单独报告，不并入基线通过率。主轮运行期间做了等价函数拆分与格式整理；之后针对q086、q096另修复了模型把动作附在业务名后的情况。这两处最终修复已通过检索复放和本地测试，并列入日期版本的真实生成补测。', '',
       '自动检查覆盖状态、完整关键值、必需引用文件及指定范围不越界；它不能替代逐句语义核验。两业务参数映射、环境参数和部分答案已人工核对实际回答及引用。此处的词法事实覆盖诊断不代表模型已证明事实，回答仍经过原文引用校验。', '',
       '| 场景 | 历史通过/总数 | 本轮通过/完成 |', '|---|---:|---:|']
    for kind,ids in groups.items():
        old=[baseline[i] for i in ids if i in baseline]
        new=[byid[i] for i in ids if i in byid and i not in errors]
        lines.append(f'| {LABELS.get(kind,kind)} | {sum(r.get("pass",False) for r in old)}/{len(old)} | {sum(r.get("pass",False) for r in new)}/{len(new)} |')
    heading=[r for r in valid if r.get('heading_hit_at_5') is not None]
    table=[r for r in heading if r['kind']=='table']
    times=[r['elapsed_seconds'] for r in valid if 'elapsed_seconds' in r]
    lines+=['',f'本轮预期章节前5命中：{sum(r["heading_hit_at_5"] for r in heading)}/{len(heading)}；其中表格：{sum(r["heading_hit_at_5"] for r in table)}/{len(table)}（历史表格为0/10）。']
    if times:
        lines+=['',f'本轮单题墙钟耗时：中位数{statistics.median(times):.1f}秒，P95 {sorted(times)[min(len(times)-1,int(len(times)*.95))]:.1f}秒。并发执行受到上游响应及机器负载影响，不是吞吐压测结论。']
    lines += ['', '## 模型版本补测与后续修复', '', f'qwen3.7-flash在主轮后段返回403 AllocationQuota.FreeTierOnly。随后使用qwen3.7-flash-2026-07-15补测55道额度阻塞题及q086，当前记录{len(dated)}/56题，通过{sum(r.get("pass",False) for r in dated)}题。历史阻塞记录保留；上方统计仅把仍受阻的最新结果排除。新增20题在首轮额度耗尽前已经全部完成。', '', 'q086碧湖计费跨文档题在模型正常时失败：查询计划将碧湖计费同步处理整体作为主体。已增加基于文档名称和问题场景的有界修正，并再次验证修正后的名称确实对应可读文档；地区或数字差异不适用该修正。检索复放已找齐DLQ-8809、77、Duty-8809。完整检索复放还发现q096的旧计划把同步附在业务名后，同样已修复；两题补充复放均通过，真实生成结果见日期版本补测记录。']
    replay = list({r['id']:r for r in read('upgrade-retrieval-replay.jsonl') + read('upgrade-subject-fix-replay.jsonl')}.values())
    answerable=[r for r in replay if r.get('answerable_case')]
    lines += ['', f'最终源码检索复放进度：{len(replay)}/150；其中有答案题的预期文件、事实及范围检查通过{sum(r.get("pass",False) for r in answerable)}/{len(answerable)}。这使用保存的查询计划，只验证检索，不验证新的查询理解或回答生成；无答案题不能用空预期值判定问答正确。原始结果：upgrade-retrieval-replay.jsonl；最后两题修正的复放另存upgrade-subject-fix-replay.jsonl，报告按ID采用最新复放。']
    lines+=['','## 新问法及人工复核','','10道业务名放在句尾的口语查询，以及双业务比较、环境口语改写、业务名放在句中、不带业务名、指定EU环境、未知业务、缺失事实、部分答案、跨文档口语追问和仅业务名的概览。','',
       '人工核对：h011青禾清算4次、澄海支付3次，分别引用各自接口规范；h012生产600、测试60；h018只回答HMAC-SHA256，明确缺少楼层数证据；h020仅业务名返回有引用的业务概览。','','## 本地与数据库检查','','本地pytest：46通过、1项需独立数据库的测试默认跳过；该数据库清理测试随后在新建空库中独立通过（1/1）。Ruff与git diff --check通过。新增PostgreSQL范围验证5组通过：项目边界、私有ACL、旧版本、仅正文英文名称匹配、已删除文档；全部临时修改已回滚。','',
       '验证中第一次把既有清理测试放在已有数据的库运行，发生Embedding主键冲突；改在独立空库迁移后通过。该尝试产生的两个空项目已按ID及创建时间清除；临时数据库也已删除。','','## 每题结果','', '| ID | 类型 | 结果 | 问题 | 实际回答 | Trace ID |','|---|---|---|---|---|---|']
    def esc(v):return str(v).replace('|','\\|').replace('\n','<br>')
    for r in sorted(current+held,key=lambda r:r['id']):
        p=r.get('response',{})
        lines.append('| '+' | '.join(esc(v) for v in [r['id'],r.get('kind','error'),'环境阻塞' if r['id'] in errors else ('通过' if r.get('pass') else '未通过'),r.get('question',''),p.get('answer',r.get('error','')),p.get('trace_id','')])+' |')
    lines+=['','## 范围与限制','','本次未建设跨语言业务别名映射；正文英文名直匹配有数据库检查，但不能据此宣称原英文别名问答已全部解决。没有进行生产部署、浏览器UI验收或高并发负载测试。业务文档发现采用有界候选和现有文本匹配，大规模库的名称索引及更复杂业务歧义仍需在真实语料上评估。','',
      '## 原始记录与复现','','结果目录：evaluation/generated/stress50。日期版本结果：upgrade-dated-answers.jsonl，选题及模型记录：upgrade-dated-selected.json。upgrade-answers.jsonl、upgrade-extra-answers.jsonl、upgrade-holdout-answers.jsonl包含实际回答、引用及全部追踪。upgrade-scope-checks.json记录数据库范围验证。upgrade-summary.json保存本轮统计与源码摘要。','',
      '```sh','.venv/bin/python -m pytest -q','.venv/bin/python -m evaluation.stress50.upgrade_scope_checks','.venv/bin/python -m evaluation.stress50.upgrade_postgres_validation','.venv/bin/python -m evaluation.stress50.run answers --chat-model qwen3.7-flash --all-projects --workers 4 --ids '+','.join(q['id'] for q in cases if q['id'].startswith('q'))+' --report upgrade-answers.jsonl','.venv/bin/python -m evaluation.stress50.scenarios --chat-model qwen3.7-flash --report upgrade-extra-answers.jsonl','.venv/bin/python -m evaluation.stress50.upgrade_holdout','.venv/bin/python -m evaluation.stress50.report_upgrade','```']
    if errors:
        pending=sorted(set(errors))
        lines += ['', '仍需在最终源码上补测以下题目（保存独立文件，不覆盖原结果）：', '', '```sh', '.venv/bin/python -m evaluation.stress50.run answers --chat-model qwen3.7-flash-2026-07-15 --all-projects --workers 4 --ids '+','.join(pending)+' --report upgrade-final-retry.jsonl', '```']
    lines += ['', '日期模型补测复现：', '', '```sh', '.venv/bin/python -m evaluation.stress50.run answers --chat-model qwen3.7-flash-2026-07-15 --all-projects --workers 4 --ids '+','.join(json.loads((OUTPUT/'upgrade-dated-selected.json').read_text())['ids'])+' --report upgrade-dated-answers.jsonl', '```']
    (OUTPUT/'upgrade-report.md').write_text('\n'.join(lines)+'\n')
    summary={'complete':complete,'planned':len(cases),'attempted':len(byid),'completed':len(valid),'passed':total_pass,'dated_model':'qwen3.7-flash-2026-07-15','dated_completed':len(dated),'dated_passed':sum(r.get('pass',False) for r in dated),'heldout_completed':len(held),'heldout_passed':held_pass,'environment_errors':errors,'missing':[q['id'] for q in cases if q['id'] not in byid], 'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'app/services/retrieval.py',ROOT/'app/integrations/search.py',ROOT/'app/services/answering.py',ROOT/'app/services/answer_contract.py']}}
    (OUTPUT/'upgrade-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k not in ['source_sha256','missing']}))

if __name__=='__main__':main()
