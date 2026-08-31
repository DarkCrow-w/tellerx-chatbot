"""Operator command for explicit OpenAI-compatible model connectivity diagnostics."""

from __future__ import annotations

import argparse
import json

from app.core.container import model_client
from app.integrations.openai_client import ModelAPIError


def _safe_error(component: str, exc: ModelAPIError, **fields: object) -> str:
    """只输出可操作的 API 元数据，不记录凭证或供应商响应正文。"""
    return json.dumps(
        {
            "component": component,
            "status": "failed",
            **fields,
            "http_status": exc.status_code,
            "error_code": exc.code,
        },
        ensure_ascii=False,
    )


def diagnostics_main() -> None:
    """显式探测向量和聊天接口，并以退出码汇总失败。"""

    parser = argparse.ArgumentParser(
        description="Run minimal OpenAI-compatible embedding and chat diagnostics"
    )
    parser.add_argument("--chat-model", default="qwen3.5-122B")
    parser.add_argument("--skip-chat", action="store_true")
    parser.add_argument("--skip-embedding", action="store_true")
    args = parser.parse_args()
    client = model_client()
    failures = 0
    if not args.skip_embedding:
        try:
            vectors, usage = client.embeddings(["企业知识库 API connectivity check"])
            print(
                json.dumps(
                    {
                        "component": "embedding",
                        "status": "ok",
                        "dimension": len(vectors[0]),
                        "tokens": usage.total_tokens,
                    },
                    ensure_ascii=False,
                )
            )
        except ModelAPIError as exc:
            failures += 1
            print(_safe_error("embedding", exc))
    print(json.dumps({"component": "rerank", "status": "disabled"}))
    if not args.skip_chat:
        try:
            result = client.chat_json(
                model_id=args.chat_model,
                system_prompt="Return JSON only.",
                user_prompt='Return {"status":"ok"}.',
                max_tokens=30,
            )
            print(
                json.dumps(
                    {
                        "component": "chat",
                        "status": "ok",
                        "model": result.model_id,
                        "tokens": result.usage.total_tokens,
                    },
                    ensure_ascii=False,
                )
            )
        except ModelAPIError as exc:
            failures += 1
            print(_safe_error("chat", exc, model=args.chat_model))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    diagnostics_main()
