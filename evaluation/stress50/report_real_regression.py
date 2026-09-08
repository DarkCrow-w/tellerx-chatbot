"""Summarize one real regression run without combining it with historical passes."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import defaultdict

from evaluation.stress50.report_scenarios import LABELS
from evaluation.stress50.run import OUTPUT, ROOT


def classify_results(latest, cases, expected_models):
    """Exclude provider failures even if a refusal accidentally passes answer checks."""
    groups = defaultdict(list)
    failures = []
    unavailable = []
    mismatched_models = []
    for case_id, record in latest.items():
        plan = record.get("retrieval", {}).get("query_understanding", {})
        response = record.get("response", {})
        provider_failed = plan.get(
            "fallback_reason"
        ) == "NoModelAvailable" or "生成模型当前不可用" in response.get("answer", "")
        if record.get("error") or provider_failed:
            unavailable.append(case_id)
        else:
            groups[cases[case_id]["kind"]].append(record)
            if not record.get("pass"):
                failures.append(case_id)
        for model in (plan.get("model_id"), response.get("model_id")):
            if model and model != expected_models[case_id]:
                mismatched_models.append(case_id)
    return groups, failures, unavailable, mismatched_models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="JSONL filename under generated/stress50")
    parser.add_argument("--supplement", action="append", default=[])
    parser.add_argument("--output", help="Separate output stem for a combined report")
    args = parser.parse_args()
    path = OUTPUT / args.report
    metadata = json.loads(path.with_suffix(".meta.json").read_text())
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    latest = {record["id"]: record for record in records}
    expected_models = {key: metadata["model"] for key in latest}
    input_paths = [path]
    for name in args.supplement:
        supplement = OUTPUT / name
        extra_meta = json.loads(supplement.with_suffix(".meta.json").read_text())
        if extra_meta["source_sha256"] != metadata["source_sha256"]:
            raise ValueError("Supplement was run on different application source")
        for line in supplement.read_text().splitlines():
            record = json.loads(line)
            latest[record["id"]] = record
            expected_models[record["id"]] = extra_meta["model"]
        input_paths.append(supplement)
    output_path = OUTPUT / args.output if args.output else path
    cases = {case["id"]: case for case in metadata["cases"]}
    groups, failures, unavailable, mismatched_models = classify_results(
        latest, cases, expected_models
    )
    changed_sources = [
        name
        for name, digest in metadata["source_sha256"].items()
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest
    ]
    valid = {key: record for key, record in latest.items() if key not in unavailable}
    standard = [record for key, record in valid.items() if not key.startswith("h")]
    held = [record for key, record in valid.items() if key.startswith("h")]
    summary = {
        "model": metadata["model"],
        "planned": len(cases),
        "attempted": len(latest),
        "completed": len(valid),
        "passed": sum(bool(record.get("pass")) for record in valid.values()),
        "standard_passed": sum(bool(record.get("pass")) for record in standard),
        "standard_completed": len(standard),
        "holdout_passed": sum(bool(record.get("pass")) for record in held),
        "holdout_completed": len(held),
        "models": {
            model: sum(expected_models[key] == model for key in valid)
            for model in set(expected_models.values())
        },
        "failures": failures,
        "unavailable": unavailable,
        "missing": sorted(set(cases) - set(latest)),
        "model_mismatch": sorted(set(mismatched_models)),
        "source_changed": changed_sources,
    }
    summary["complete"] = (
        len(latest) == len(cases)
        and not unavailable
        and not changed_sources
        and not mismatched_models
    )
    summary["all_passed"] = summary["complete"] and not failures
    lines = [
        "# 重构后完整真实问答测试",
        "",
        f"模型与有效题数：{summary['models']}。开始时间：{metadata['started_at']}。",
        "",
        f"本轮有效完成 {len(valid)}/{len(cases)}，自动检查通过 {summary['passed']} 题。标准题 {summary['standard_passed']}/{len(standard)}；新增问法 {summary['holdout_passed']}/{len(held)}。",
        "",
        "本轮每道题重新执行查询理解、检索、模型生成与引用校验，未复用旧答案或以历史通过结果补齐。简单查询可以按原逻辑跳过模型理解；无证据时可以直接拒答。调用与 `/api/v1/chat` 相同的应用服务，搜索范围为全部可访问知识库。",
        "",
        "使用原 50 份文件及现有索引，Embedding 为 qwen3.7-text-embedding-flash/1024 维，Rerank 关闭。继续排除原先暂缓的 10 道英文别名题。",
        "",
        "检查项为回答状态、预期关键值、必需引用文件、指定文档范围和越界证据。自动检查不能替代对每个句子的语义核验，也不代表对所有真实用户问法的准确率。",
        "",
        "| 场景 | 通过/完成 |",
        "|---|---:|",
    ]
    for kind, rows in groups.items():
        lines.append(
            f"| {LABELS.get(kind, kind)} | {sum(bool(row.get('pass')) for row in rows)}/{len(rows)} |"
        )
    times = sorted(
        record["elapsed_seconds"] for record in valid.values() if "elapsed_seconds" in record
    )
    if times:
        lines += [
            "",
            f"单题耗时中位数 {statistics.median(times):.1f} 秒，P95 {times[min(len(times) - 1, int(len(times) * 0.95))]:.1f} 秒。4 路并发，非吞吐压测。",
        ]
    lines += [
        "",
        "## 未通过或未完成",
        "",
        f"未通过：{', '.join(failures) or '无'}。环境异常：{', '.join(unavailable) or '无'}。尚未完成：{', '.join(summary['missing']) or '无'}。",
        "",
        f"模型不一致：{summary['model_mismatch']}。执行期间源码变化：{changed_sources}。",
        "",
        "## 每题记录",
        "",
        "| ID | 结果 | 未通过检查 | 回答 |",
        "|---|---|---|---|",
    ]
    for key in sorted(latest):
        record = latest[key]
        answer = record.get("response", {}).get("answer", record.get("error", ""))
        failed = [name for name, passed in record.get("checks", {}).items() if not passed]
        answer = str(answer).replace("|", "\\|").replace("\n", "<br>")
        lines.append(
            f"| {key} | {'环境阻塞' if key in unavailable else ('通过' if record.get('pass') else '未通过')} | {', '.join(failed)} | {answer} |"
        )
    lines += [
        "",
        f"原始完整回答、引用、Trace 和耗时：`{path.name}`。问题快照及源码 SHA256：`{path.with_suffix('.meta.json').name}`。",
        "",
    ]
    lines += [
        "输入记录（按顺序采用每题最新结果）：" + "、".join(item.name for item in input_paths),
        "",
    ]
    if args.supplement:
        lines += [
            "本报告合并本轮两个模型阶段，不能视为任一模型独立完成全部题目。先前额度失败记录保留在原文件中。",
            "",
        ]
    output_path.with_suffix(".md").write_text("\n".join(lines))
    output_path.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
