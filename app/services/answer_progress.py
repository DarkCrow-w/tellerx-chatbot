"""Request-local progress reporting, independent of the HTTP transport."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_listener: ContextVar[Callable[[dict], None] | None] = ContextVar("answer_progress", default=None)


class AnswerCancelled(Exception):
    """The client stopped waiting; stop work at the next stage boundary."""


@contextmanager
def report_progress(listener: Callable[[dict], None]) -> Iterator[None]:
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)


def progress(stage: str, text: str, details: list[str] | None = None) -> None:
    listener = _listener.get()
    if listener is not None:
        event = {"stage": stage, "text": text}
        if details:
            event["details"] = list(
                dict.fromkeys(" ".join(item.split())[:120] for item in details if item.strip())
            )[:3]
        listener(event)
