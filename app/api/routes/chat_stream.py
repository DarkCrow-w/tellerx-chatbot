"""Stream real execution stages while the existing answer contract stays intact."""

import asyncio
import json
import logging
from threading import Event

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.contracts.schemas import ChatRequest
from app.core.container import chat_application_service
from app.db import SessionLocal
from app.services.answer_progress import AnswerCancelled, report_progress

router = APIRouter()
logger = logging.getLogger(__name__)
_pending_workers: set[asyncio.Task] = set()


def encode_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def answer_events(request: ChatRequest):
    loop = asyncio.get_running_loop()
    # Only stage transitions are queued, never tokens or model reasoning.
    queue: asyncio.Queue = asyncio.Queue()
    stopped = Event()

    def send(event: str, data: dict):
        if stopped.is_set():
            raise AnswerCancelled()
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def work():
        try:
            # The session is created, used and closed in the same worker thread.
            with SessionLocal() as db, report_progress(lambda data: send("stage", data)):
                send("stage", {"stage": "started", "text": "已开始处理问题"})
                result = chat_application_service().answer(db, **request.model_dump())
                send("final", result.model_dump(mode="json"))
        except AnswerCancelled:
            pass
        except Exception:
            logger.exception("流式问答失败")
            if not stopped.is_set():
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("error", {"detail": "本次问答未能完成，请稍后重试。"}),
                )

    # Starlette's worker pool bounds simultaneous blocking work.
    task = asyncio.create_task(run_in_threadpool(work))
    _pending_workers.add(task)
    task.add_done_callback(_pending_workers.discard)
    try:
        yield encode_event("stage", {"stage": "accepted", "text": "已收到问题，等待处理"})
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            yield encode_event(event, data)
            if event in {"final", "error"}:
                break
    finally:
        stopped.set()
        # Blocking provider calls finish under their existing timeout; the next
        # progress boundary prevents starting another stage after disconnect.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    return StreamingResponse(
        answer_events(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
