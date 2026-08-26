"""问答 HTTP 用例编排。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sqlalchemy.orm import Session

from app.application.errors import InvalidRequestError, ResourceNotFoundError, UpstreamServiceError
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
    ) -> ChatResponse:
        """确定知识库范围后执行证据约束问答。"""

        scope = list(dict.fromkeys(project_ids))
        if not scope:
            # 只读取两个 ID 即可判断是否存在歧义，无需加载全部项目。
            available = self.repository.list_project_ids(db, limit=2)
            if len(available) > 1:
                raise InvalidRequestError("存在多个知识库项目，请先选择一个项目再提问。")
            scope = available
        try:
            return self.answering_provider().answer(
                db,
                question=question.strip(),
                project_ids=scope,
                conversation_id=conversation_id,
                pinned_model=pinned_model,
            )
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
