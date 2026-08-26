"""问答、会话和人工反馈的数据访问。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AnswerFeedback,
    Chunk,
    Conversation,
    Document,
    DocumentVersion,
    Message,
    Project,
    QueryTrace,
)


class ChatRepository:
    """封装问答用例涉及的关系数据库读写。"""

    def list_project_ids(self, db: Session, *, limit: int) -> list[str]:
        """按项目名称返回有限数量的项目 ID，用于自动确定问答范围。"""

        return list(db.scalars(select(Project.id).order_by(Project.name).limit(limit)))

    def get_or_create_conversation(
        self, db: Session, conversation_id: str | None
    ) -> Conversation:
        """复用已有会话；未提供或不存在时创建新会话。"""

        conversation = db.get(Conversation, conversation_id) if conversation_id else None
        if conversation is None:
            conversation = Conversation()
            db.add(conversation)
            db.flush()
        return conversation

    def live_searchable_chunk_ids(self, db: Session, chunk_ids: set[str]) -> set[str]:
        """返回当前仍可检索的分块 ID，供落库前消除版本切换竞态。"""

        if not chunk_ids:
            return set()
        rows = db.execute(
            select(
                Chunk.id,
                DocumentVersion.lifecycle_status,
                DocumentVersion.technical_status,
                DocumentVersion.is_current,
                Document.is_deleted,
            )
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .where(Chunk.id.in_(chunk_ids))
        ).all()
        return {
            chunk_id
            for chunk_id, lifecycle, technical, is_current, is_deleted in rows
            if not is_deleted
            and technical == "searchable"
            and (lifecycle == "draft" or (lifecycle == "approved" and is_current))
        }

    def save_exchange(
        self,
        db: Session,
        *,
        conversation: Conversation,
        question: str,
        answer: str,
        answer_status: str,
        model_id: str | None,
        trace_id: str,
        citations: list[dict],
        normalized_query: str,
        project_ids: list[str],
        index_name: str,
        retrieval_json: dict,
        latency_ms: float,
    ) -> Message:
        """在同一事务中保存用户消息、助手消息和查询追踪。"""

        db.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content=question,
                trace_id=trace_id,
            )
        )
        assistant_message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            answer_status=answer_status,
            model_id=model_id,
            trace_id=trace_id,
            citations=citations,
        )
        db.add(assistant_message)
        db.add(
            QueryTrace(
                trace_id=trace_id,
                normalized_query=normalized_query,
                project_ids=project_ids,
                index_name=index_name,
                retrieval_json=retrieval_json,
                answer_status=answer_status,
                model_id=model_id,
                latency_ms=latency_ms,
            )
        )
        db.commit()
        return assistant_message

    def add_feedback(
        self,
        db: Session,
        *,
        message_id: str,
        rating: str,
        comment: str | None,
    ) -> AnswerFeedback | None:
        """仅当目标是助手消息时保存反馈，否则返回 ``None``。"""

        message = db.get(Message, message_id)
        if message is None or message.role != "assistant":
            return None
        feedback = AnswerFeedback(message_id=message_id, rating=rating, comment=comment)
        db.add(feedback)
        db.commit()
        return feedback
