"""Evidence-bound chat and human feedback endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.contracts.schemas import ChatRequest, ChatResponse, FeedbackIn
from app.core.container import answer_service
from app.db import get_db
from app.db.models import AnswerFeedback, Message
from app.services.answer_contract import AnswerValidationError
from app.services.model_router import NoModelAvailable

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    """Retrieve evidence, route one Qwen model, and return validated citations."""

    try:
        return answer_service().answer(
            db,
            question=request.question.strip(),
            project_ids=request.project_ids,
            conversation_id=request.conversation_id,
            pinned_model=request.pinned_model,
        )
    except NoModelAvailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except AnswerValidationError as exc:
        raise HTTPException(502, f"Answer validation failed: {exc}") from exc


@router.post("/feedback", status_code=201)
def submit_feedback(payload: FeedbackIn, db: Session = Depends(get_db)) -> dict:
    """Attach human feedback to a persisted assistant message."""

    message = db.get(Message, payload.message_id)
    if not message or message.role != "assistant":
        raise HTTPException(404, "Assistant message not found")
    feedback = AnswerFeedback(
        message_id=payload.message_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(feedback)
    db.commit()
    return {"id": feedback.id, "status": "recorded"}
