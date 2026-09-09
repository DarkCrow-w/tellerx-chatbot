"""Streaming reports real stages before completion and stops at safe boundaries."""

import asyncio
from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace

from app.api.routes import chat_stream
from app.contracts.schemas import ChatRequest
from app.services.answer_progress import progress, report_progress


def test_progress_listener_is_scoped():
    events = []
    with report_progress(events.append):
        progress("retrieving", "检索")
    progress("saving", "保存")
    assert events == [{"stage": "retrieving", "text": "检索"}]


def test_stages_precede_final_and_session_closes(monkeypatch):
    release = Event()
    closed = Event()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.set()

    def answer(db, **kwargs):
        progress("retrieving", "检索文档")
        assert release.wait(5)
        return SimpleNamespace(model_dump=lambda **kw: {"answer": "答案", "sources": []})

    monkeypatch.setattr(chat_stream, "SessionLocal", Session)
    monkeypatch.setattr(
        chat_stream, "chat_application_service", lambda: SimpleNamespace(answer=answer)
    )

    async def run():
        stream = chat_stream.answer_events(ChatRequest(question="问题", project_ids=[]))
        try:
            assert "accepted" in await anext(stream)
            assert "started" in await anext(stream)
            assert "retrieving" in await asyncio.wait_for(anext(stream), 2)
            release.set()
            assert "event: final" in await asyncio.wait_for(anext(stream), 2)
        finally:
            release.set()
            await stream.aclose()
        assert closed.wait(2)

    asyncio.run(run())


def test_disconnect_stops_next_stage(monkeypatch):
    release = Event()
    finished = Event()
    persisted = []

    def answer(db, **kwargs):
        try:
            progress("generating", "生成")
            assert release.wait(5)
            progress("saving", "保存")
            persisted.append(True)
        finally:
            finished.set()

    monkeypatch.setattr(chat_stream, "SessionLocal", lambda: nullcontext())
    monkeypatch.setattr(
        chat_stream, "chat_application_service", lambda: SimpleNamespace(answer=answer)
    )

    async def run():
        stream = chat_stream.answer_events(ChatRequest(question="问题", project_ids=[]))
        assert "accepted" in await anext(stream)
        assert "started" in await anext(stream)
        assert "generating" in await anext(stream)
        await stream.aclose()
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert not persisted

    asyncio.run(run())


def test_failure_sends_terminal_error_without_internal_details(monkeypatch):
    def answer(db, **kwargs):
        raise RuntimeError("private-provider-detail")

    monkeypatch.setattr(chat_stream, "SessionLocal", lambda: nullcontext())
    monkeypatch.setattr(
        chat_stream, "chat_application_service", lambda: SimpleNamespace(answer=answer)
    )

    async def run():
        events = [
            event
            async for event in chat_stream.answer_events(
                ChatRequest(question="问题", project_ids=[])
            )
        ]
        assert "event: error" in events[-1]
        assert "private-provider-detail" not in "".join(events)

    asyncio.run(run())
