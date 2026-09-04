"""问答与人工反馈 Controller。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.error_mapping import run_application
from app.contracts.schemas import ChatRequest, ChatResponse, FeedbackIn
from app.core.container import chat_application_service
from app.db import get_db

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    """接收问答请求；业务范围、检索和生成均交由应用层处理。"""

    return run_application(
        lambda: chat_application_service().answer(
            db,
            question=request.question,
            project_ids=request.project_ids,
            conversation_id=request.conversation_id,
            pinned_model=request.pinned_model,
            document_id=request.document_id,
            document_hint=request.document_hint,
            section_path=request.section_path,
        )
    )


@router.post("/feedback", status_code=201)
def submit_feedback(payload: FeedbackIn, db: Session = Depends(get_db)) -> dict[str, str]:
    """接收人工反馈；消息校验和持久化由应用层负责。"""

    return run_application(lambda: chat_application_service().submit_feedback(db, payload))
