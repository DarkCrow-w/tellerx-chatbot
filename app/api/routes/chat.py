"""Evidence-bound chat and human feedback endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.schemas import ChatRequest, ChatResponse, FeedbackIn
from app.core.container import answer_service
from app.db import get_db
from app.db.models import AnswerFeedback, Message, Project
from app.services.answer_contract import AnswerValidationError
from app.services.model_router import NoModelAvailable

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    """检索证据、路由千问模型，并只返回通过校验的引用。"""

    project_ids = list(dict.fromkeys(request.project_ids))
    if not project_ids:
        available = list(db.scalars(select(Project.id).order_by(Project.name).limit(2)))
        if len(available) > 1:
            raise HTTPException(422, "存在多个知识库项目，请先选择一个项目再提问。")
        project_ids = available
    try:
        return answer_service().answer(
            db,
            question=request.question.strip(),
            project_ids=project_ids,
            conversation_id=request.conversation_id,
            pinned_model=request.pinned_model,
        )
    except NoModelAvailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except AnswerValidationError as exc:
        raise HTTPException(502, f"Answer validation failed: {exc}") from exc


@router.post("/feedback", status_code=201)
def submit_feedback(payload: FeedbackIn, db: Session = Depends(get_db)) -> dict:
    """把人工反馈关联到已持久化的助手消息。"""

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
