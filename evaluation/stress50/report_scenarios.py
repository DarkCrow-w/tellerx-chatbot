"""Build an auditable Markdown report from fresh real-model scenario results."""
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from evaluation.stress50.run import OUTPUT

LABELS = dict(filename_heading='文件名＋多级标题', filename_content='文件名＋内容',full_filename='完整文件名＋内容', table='文件内表格及行列约束',global_single='业务名称全库检索',cross_document='跨文档关联',global_english='英文业务名全库检索',structure_tree='树状结构父子关系',ambiguous='文件名歧义澄清',scope_insufficient='限定文档内无答案',long_document_tail='长文档末尾定位',independent_global='独立服务全库检索',true_conflict='同范围事实冲突',different_scope='生产与测试范围区分',consistent_facts='重复一致事实',missing_document='不存在的文档',missing_fact='不存在的事实',simple_document_global='Markdown/HTML/TXT全库检索',identifier_only_global='仅队列编号全库检索',reverse_content_global='仅负责人反向查参数')

def main():
    cases = json.loads((OUTPUT/'questions.json').read_text()) + json.loads((OUTPUT/'scenario-extra-questions.json').read_text())
    rows = []
    for name in ['scenario-answers.jsonl','scenario-extra-answers.jsonl']:
        rows += [json.loads(l) for l in (OUTPUT/name).read_text().splitlines() if l]
    initial_rows = list(rows)
    flash_path = OUTPUT/'scenario-flash-answers.jsonl'
    flash_rows = [json.loads(l) for l in flash_path.read_text().splitlines() if l] if flash_path.exists() else []
    rows += flash_rows
    by_id = {r['id']: r for r in rows}
    rows = list(by_id.values())
    missing = [c['id'] for c in cases if c['id'] not in by_id]
    groups = defaultdict(list)
    for r in by_id.values(): groups[r['kind'] if 'kind' in r else next(c['kind'] for c in cases if c['id']==r['id'])].append(r)
    blocked = set(json.loads((OUTPUT/'scenario-environment-blocks.json').read_text())['affected_ids']) if (OUTPUT/'scenario-environment-blocks.json').exists() else set()
    for r in flash_rows:
        unavailable = (r.get('error') or r.get('retrieval',{}).get('query_understanding',{}).get('fallback_reason') == 'NoModelAvailable' or '生成模型当前不可用' in r.get('response',{}).get('answer',''))
        if unavailable: blocked.add(r['id'])
        else: blocked.discard(r['id'])
    valid = [r for r in by_id.values() if r['id'] not in blocked]
    passed = sum(r.get('pass',False) for r in valid)
    lines = ['# 50份文档：知识查询实测报告', '', '测试日期：2026-09-07。', '',
      f'计划并尝试 {len(cases)} 题；有效完成 {len(valid)} 题，通过 {passed} 题，功能未通过 {len(valid)-passed} 题；有效样本通过率 {passed/max(1,len(valid)):.1%}。当前仍受外部模型额度限制的题目为 {len(blocked)} 道。', '',
      '## 模型切换与统计口径', '',
      f'先使用Max模型获得129道有效结果；按用户要求改用qwen3.7-flash，计划补跑31道额度阻塞题并复测16道功能失败题，共47道。Flash目前记录{len(flash_rows)}道。汇总表按每题最新结果去重，保留Max已通过的113道；这是分阶段混合模型结果，不是单一模型的160题通过率。原Max报告见scenario-max-report.md。', '',
      '## 外部阻塞与错误呈现', '',
      'Qwen在执行后段持续返回 HTTP 403 / AllocationQuota.FreeTierOnly。q115、q116在生成阶段失败；q117—q145的查询理解也进入NoModelAvailable回退。首轮共31题标为环境阻塞，包含原始自动检查误判为通过的q134、q135。本轮没有把此前 answers.jsonl 的旧结果替代新测试结果。', '',
      '新增问题：模型不可用时文案正确提示“已找到相关资料，但生成模型当前不可用，因此本次不生成结论。”，但状态仍为 insufficient_evidence，导致自动统计容易将服务故障误记为知识不足。例如q117已经召回末尾口令，但模型额度错误导致拒答。app/services/answering.py 的 _generate_answer 捕获 NoModelAvailable 后最终走证据不足回复；建议将上游服务不可用与知识不足分为不同错误状态。', '',
      '后续已按用户要求切换qwen3.7-flash补测；文档、索引和向量模型保持相同。历史额度阻塞记录保留在scenario-environment-blocks.json，最新有效结果以Flash补测文件为准。', '',
      '## 执行范围与判定', '',
      '使用50份已生成文档（40 DOCX、4 Markdown、3 HTML、3 TXT）及隔离测试数据库；入库审计为50个当前文档版本、1,965个文本块、1,965个向量、1,922个非根章节。', '',
      '真实调用与 `/chat` 路由相同的 `ChatApplicationService.answer`，经过查询理解、PostgreSQL全文与向量检索、证据选择、Qwen生成、引用校验和数据库追踪。未通过浏览器或HTTP传输层；本报告评价后端问答链路。所有问题传入空 project_ids（全部知识库），没有预传 document_id、document_hint 或 section_path，文件与标题识别均依赖用户问题。隔离库中的全库范围不能证明多租户权限隔离。', '',
      '问答模型：首轮qwen3.7-max-2026-06-08，补测qwen3.7-flash；向量模型：qwen3.7-text-embedding-flash，1024维；rerank关闭；并发4（补充题并发2）。此配置是本次测试配置，不代表其他部署配置的效果。', '',
      '通过条件为预期状态、关键答案值、必需文件引用、指定文件范围以及证据不越界全部满足。数字按完整数字边界匹配；其他值按不区分大小写包含匹配。章节前5命中及证据事实覆盖另列诊断，不计入基础通过率。自动检查无法全面证明语义正确或引用逐句蕴含，单次运行也不衡量稳定性。', '',
      '## 各类结果', '', '| 场景 | 通过/有效完成 | 通过率 | 环境阻塞 |', '|---|---:|---:|---:|']
    for k, rs in groups.items():
        ok = [r for r in rs if r['id'] not in blocked]
        n = sum(r.get('pass',False) for r in ok)
        rate = f'{n/len(ok):.1%}' if ok else '待补测'
        lines.append(f'| {LABELS.get(k,k)} | {n}/{len(ok)} | {rate} | {len(rs)-len(ok)} |')
    if flash_rows:
        resolved_flash=[r for r in flash_rows if r['id'] not in blocked]
        lines += ['', f'Flash补测单独统计：有效{len(resolved_flash)}题，通过{sum(r.get("pass",False) for r in resolved_flash)}题。', '', '## Max失败题的Flash复测', '', '| ID | 问题 | Max | Flash |', '|---|---|---|---|']
        original_failed={r['id']:r for r in initial_rows if not r.get('pass') and r['id'] not in {f'q{i:03d}' for i in range(115,146)}}
        for r in flash_rows:
            if r['id'] in original_failed:
                state='环境阻塞' if r['id'] in blocked else ('通过' if r.get('pass') else '未通过')
                lines += [f'| {r["id"]} | {r.get("question","")} | 未通过 | {state} |']
    lines += ['', '## 结果解释与改进方向', '',
      '需把“识别到业务或文件”与“召回了问题所需事实”分开验收。未通过案例若已正确解析范围却缺少答案值，优先排查候选召回、章节选择与证据预算；若证据已完整再检查生成和引用。', '',
      '建议后续围绕失败题做回归：英文别名应关联到整个文档及其子章节；全库查询按所问事实选择章节；长文档中避免重复运行参考段落挤占表格及业务参数；同时保留显式文件范围、歧义澄清和无证据拒答。上述为根据实测提出的改进方向，本轮未修改生产检索逻辑，也未证明某个具体改法已经有效。', '',
      '附加检索复放诊断见 scenario-retrieval-diagnostics.json：使用已记录查询计划重新执行检索，记录最终候选列表中的预期值命中。诊断是单独的检索复放，与主测试的原始 trace 分开保存。', '',
      '## 测试边界', '',
      '本轮是50份合成文档的单轮后端问答测试，未覆盖浏览器交互、HTTP传输、真实业务语料、多轮代词追问、用户权限隔离、旧新版本切换、OCR图片内文字、恶意文档指令和高并发负载；这些不能由本次通过率推断。原文件预期值核对记录见 scenario-fixture-audit.json。']
    durations = [r['elapsed_seconds'] for r in valid if 'elapsed_seconds' in r]
    if durations:
        lines += ['',f'单题耗时：中位数 {statistics.median(durations):.1f} 秒；P95 {sorted(durations)[min(len(durations)-1,int(len(durations)*.95))]:.1f} 秒（并发执行下的墙钟时间）。']
    heading = [r for r in valid if r.get('heading_hit_at_5') is not None]
    lines += ['',f'显式预期章节题的证据前5命中：{sum(r["heading_hit_at_5"] for r in heading)}/{len(heading)}。']
    for kind in ['filename_heading','table','long_document_tail']:
        subset=[r for r in heading if r.get('kind')==kind]
        if subset:
            lines += [f'{LABELS[kind]}：前5证据章节命中 {sum(r["heading_hit_at_5"] for r in subset)}/{len(subset)}；基础回答通过 {sum(r.get("pass",False) for r in subset)}/{len(subset)}。']
    lines += ['', '表格题即使回答通过，也要关注相关表格是否排在前5证据之外；例如 q004 的目标表格出现在第9个证据位置。这说明最终答案正确并不等于章节排序理想，邻居补充可能对结果有关键作用。']
    if missing: lines += ['',f'未完成题目：{", ".join(missing)}。']
    lines += ['', '## 未通过案例与诊断', '']
    for r in rows:
        if r.get('pass') or r['id'] in blocked: continue
        response=r.get('response',{})
        ev=r.get('evidence',[])
        checks=', '.join(k for k,v in r.get('checks',{}).items() if not v)
        cause = ('未召回完整预期事实，优先检查检索及证据筛选。' if r.get('retrieval_all_values') is False else '需结合证据、回答及引用检查生成或判定。')
        lines += [f'### {r["id"]} · {LABELS.get(r.get("kind"),r.get("kind","执行错误"))}', '',
          f'问题：{r.get("question", "见题集")}','',f'预期：{r.get("expected_status")}；关键值：{", ".join(r.get("expected_values",[]))}。', '',
          f'实际：{response.get("status",r.get("error"))}；未满足：{checks}。', '',f'回答：{response.get("answer", "执行异常")}','',f'诊断：{cause}', '',
          '实际证据章节：'+('；'.join(dict.fromkeys(e['filename']+' / '+(e['heading'] or '根节点') for e in ev)) or '无'), '']
    lines += ['## 每题实际回答与追踪', '', '| ID | 场景 | 结果 | 问题 | 实际回答 | 引用文件 | Trace ID |', '|---|---|---|---|---|---|---|']
    def esc(s):return str(s).replace('|','\\|').replace('\n','<br>')
    for r in sorted(rows,key=lambda r:r['id']):
        p=r.get('response',{})
        lines.append('| '+' | '.join(esc(v) for v in [r['id'],LABELS.get(r.get('kind'),r.get('kind','')), '环境阻塞' if r['id'] in blocked else ('通过' if r.get('pass') else '未通过'), r.get('question',''), p.get('answer',r.get('error','')), ', '.join(dict.fromkeys(x['filename'] for x in p.get('sources',[]))),p.get('trace_id','')])+' |')
    lines += ['', '## 复现与原始记录', '', '```sh', '.venv/bin/python -m evaluation.stress50.run audit', '.venv/bin/python -m evaluation.stress50.run answers --all-projects --workers 4 --report scenario-answers.jsonl', '.venv/bin/python -m evaluation.stress50.scenarios', '.venv/bin/python -m evaluation.stress50.report_scenarios', '.venv/bin/python -m evaluation.stress50.run answers --chat-model qwen3.7-flash --all-projects --workers 4 --ids ' + ','.join(json.loads((OUTPUT/'scenario-flash-selected.json').read_text())['ids']) + ' --report scenario-flash-answers.jsonl', '```', '', 'Flash补测原始记录：scenario-flash-answers.jsonl，选题及模型记录：scenario-flash-selected.json。原始题集：questions.json、scenario-extra-questions.json。原始结果：scenario-answers.jsonl、scenario-extra-answers.jsonl（包含回答、证据正文、查询计划、引用与trace_id）。路径均相对于 evaluation/generated/stress50。']
    if blocked:
        retry = '.venv/bin/python -m evaluation.stress50.run answers --all-projects --workers 4 --ids ' + ','.join(sorted(blocked)) + ' --report scenario-quota-retry.jsonl'
        lines += ['', '额度恢复后的补测命令（保存为新文件，不覆盖本轮原始结果；不要使用 --resume，因为原脚本会跳过无error字段的降级回复）：', '', '```sh', retry, '```']
    (OUTPUT/'scenario-report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'planned':len(cases),'attempted':len(by_id),'valid':len(valid),'blocked':len(blocked),'passed':passed,'missing':missing},ensure_ascii=False))

if __name__=='__main__': main()
