"""Additional content-only questions; uses the real chatbot application and isolated fixture DB."""
import json
from pathlib import Path
from types import SimpleNamespace
from evaluation.stress50 import run


def cases():
    manifest = json.loads((run.OUTPUT / 'manifest.json').read_text())
    specs = manifest['documents']
    result = []
    for i in range(1, 11):
        result.append(dict(id=f'content{i:02d}', kind='identifier_only_global',
            question=f'DLQ-{8800+i} 队列积压达到多少条需要告警，通知哪个值班角色？',
            expected_files=[specs[(i-1)*3+2]['filename']],
            values=[str(50+i*3), f'Duty-{8800+i}'], status='answered'))
    for i in range(1, 6):
        result.append(dict(id=f'reverse{i:02d}', kind='reverse_content_global',
            question=f'Owner-{9000+i} 负责审批的服务，每批最多能处理多少条记录？',
            expected_files=[specs[30+i-1]['filename']], values=[str(200+i*13)], status='answered'))
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--chat-model', default='qwen3.7-max-2026-06-08')
    parser.add_argument('--report', default='scenario-extra-answers.jsonl')
    args = parser.parse_args()
    run.configure(SimpleNamespace(model_env=Path('/Users/cliff/Documents/TellerxChatBot/.env'),
        connection_doc=Path('/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md'),
        key_file=Path('/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt'),
        embedding_model='qwen3.7-text-embedding-flash', chat_model=args.chat_model))
    items = cases()
    (run.OUTPUT / 'scenario-extra-questions.json').write_text(json.dumps(items, ensure_ascii=False, indent=2))
    run.run_jobs(items, lambda q: run.answer_one(q, None, True),
        run.OUTPUT / args.report, 2, False)
