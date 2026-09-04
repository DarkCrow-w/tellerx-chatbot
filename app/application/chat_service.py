"""问答 HTTP 用例编排。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sqlalchemy.orm import Session

from app.application.errors import ResourceNotFoundError, UpstreamServiceError
from app.contracts.schemas import ChatResponse, FeedbackIn
from app.repositories.chat import ChatRepository
from app.services.answer_contract import AnswerValidationError
from app.services.model_router import NoModelAvailable


class GroundedAnswerService(Protocol):
    """应用层使用的最小问答业务接口。"""

    def answer(
        self,
        db: Session,
        *,
        question: str,
        project_ids: list[str],
        conversation_id: str | None,
        pinned_model: str | None,
        document_id: str | None = None,
        document_hint: str | None = None,
        section_path: list[str] | None = None,
    ) -> ChatResponse: ...


class ChatApplicationService:
    """处理“提出问题”和“提交反馈”两个应用用例。"""

    def __init__(
        self,
        answering_provider: Callable[[], GroundedAnswerService],
        repository: ChatRepository,
    ):
        # 使用提供器延迟构造模型客户端：项目范围不明确时无需读取 API 密钥。
        self.answering_provider = answering_provider
        self.repository = repository

    def answer(
        self,
        db: Session,
        *,
        question: str,
        project_ids: list[str],
        conversation_id: str | None,
        pinned_model: str | None,
        document_id: str | None = None,
        document_hint: str | None = None,
        section_path: list[str] | None = None,
    ) -> ChatResponse:
        """确定知识库范围后执行证据约束问答。"""

        scope = list(dict.fromkeys(project_ids))
        # 空列表是明确的“全部知识库”范围；搜索层仍逐文档执行 ACL 约束。
        try:
            arguments = {
                "question": question.strip(),
                "project_ids": scope,
                "conversation_id": conversation_id,
                "pinned_model": pinned_model,
            }
            if document_id or document_hint or section_path:
                arguments.update(
                    document_id=document_id,
                    document_hint=document_hint,
                    section_path=section_path,
                )
            return self.answering_provider().answer(db, **arguments)
        except NoModelAvailable as exc:
            raise UpstreamServiceError(str(exc)) from exc
        except AnswerValidationError as exc:
            raise UpstreamServiceError(f"Answer validation failed: {exc}") from exc

    def submit_feedback(self, db: Session, payload: FeedbackIn) -> dict[str, str]:
        """把人工反馈关联到已持久化的助手消息。"""

        feedback = self.repository.add_feedback(
            db,
            message_id=payload.message_id,
            rating=payload.rating,
            comment=payload.comment,
        )
        if feedback is None:
            raise ResourceNotFoundError("Assistant message not found")
        return {"id": feedback.id, "status": "recorded"}
