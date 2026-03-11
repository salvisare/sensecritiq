import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Integer, DateTime, ForeignKey, ARRAY, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    project: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    # queued | processing | ready | failed
    status: Mapped[str] = mapped_column(String, default="queued", nullable=False)
    file_s3_key: Mapped[str | None] = mapped_column(String, nullable=True)
    transcript_s3_key: Mapped[str | None] = mapped_column(String, nullable=True)
    themes: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    findings: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    quote_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
