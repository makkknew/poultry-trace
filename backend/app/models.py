import enum
from datetime import datetime, timezone
from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


def now_utc():
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    OPERATOR = "OPERATOR"
    SHIFT_LEAD = "SHIFT_LEAD"
    MANAGER = "MANAGER"
    ADMIN = "ADMIN"


class Stage(str, enum.Enum):
    INCUBATION = "INCUBATION"
    GROWING = "GROWING"
    SLAUGHTER = "SLAUGHTER"
    PACKAGING = "PACKAGING"


class BatchStatus(str, enum.Enum):
    CREATED = "CREATED"
    IN_PROGRESS = "IN_PROGRESS"
    TRANSFERRED = "TRANSFERRED"
    RECEIVED = "RECEIVED"
    COMPLETED = "COMPLETED"
    DISPUTED = "DISPUTED"
    CLOSED = "CLOSED"


class TransferStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    RECEIVED = "RECEIVED"
    DISPUTED = "DISPUTED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class LossReason(str, enum.Enum):
    NATURAL_MORTALITY = "NATURAL_MORTALITY"
    DISEASE = "DISEASE"
    INJURY = "INJURY"
    TECHNICAL_LOSS = "TECHNICAL_LOSS"
    OTHER = "OTHER"


class ReviewStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(160))
    role: Mapped[Role] = mapped_column(Enum(Role), index=True)
    stage: Mapped[Stage | None] = mapped_column(Enum(Stage), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (CheckConstraint("initial_quantity >= 0"), UniqueConstraint("batch_number"))
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(40), index=True)
    stage: Mapped[Stage] = mapped_column(Enum(Stage), index=True)
    initial_quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[BatchStatus] = mapped_column(Enum(BatchStatus), default=BatchStatus.CREATED, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (CheckConstraint("sent_quantity > 0"), CheckConstraint("received_quantity IS NULL OR received_quantity >= 0"), Index("ix_transfers_stage_status", "from_stage", "status"))
    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    from_stage: Mapped[Stage] = mapped_column(Enum(Stage), index=True)
    to_stage: Mapped[Stage] = mapped_column(Enum(Stage), index=True)
    sent_quantity: Mapped[int] = mapped_column(Integer)
    received_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discrepancy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[TransferStatus] = mapped_column(Enum(TransferStatus), default=TransferStatus.DRAFT, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    receiver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmation_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Loss(Base):
    __tablename__ = "losses"
    __table_args__ = (CheckConstraint("quantity > 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    stage: Mapped[Stage] = mapped_column(Enum(Stage), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    reason: Mapped[LossReason] = mapped_column(Enum(LossReason))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    object_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_created_at", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    object_type: Mapped[str] = mapped_column(String(40))
    object_id: Mapped[str] = mapped_column(String(80))
    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class BalanceAdjustment(Base):
    __tablename__ = "balance_adjustments"
    __table_args__ = (CheckConstraint("quantity_delta != 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    stage: Mapped[Stage] = mapped_column(Enum(Stage))
    quantity_delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
