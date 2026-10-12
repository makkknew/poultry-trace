import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session
from .models import AuditLog, BalanceAdjustment, Batch, Loss, LossReason, Notification, ReviewStatus, Stage, Transfer, TransferStatus

ZERO_HASH = "0" * 64


def audit(db: Session, action: str, object_type: str, object_id: str, actor_id: int | None,
          request=None, old=None, new=None, reason=None):
    # Serialize the append operation on PostgreSQL so concurrent requests preserve one chain.
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(71930421)"))
    previous = db.scalar(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
    previous_hash = previous.event_hash if previous else ZERO_HASH
    created = datetime.now(timezone.utc)
    payload = {"action": action, "object_type": object_type, "object_id": str(object_id),
               "actor_id": actor_id, "old_value": old, "new_value": new, "reason": reason,
               "created_at": created.isoformat()}
    digest = hashlib.sha256((previous_hash + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)).encode()).hexdigest()
    client_ip = request.client.host if request and request.client else None
    forwarded_for = request.headers.get("x-forwarded-for") if request else None
    if forwarded_for:
        client_ip = forwarded_for.split(",", 1)[0].strip()
    entry = AuditLog(actor_id=actor_id, action=action, object_type=object_type, object_id=str(object_id),
                     old_value=old, new_value=new, reason=reason,
                     ip_address=client_ip,
                     user_agent=request.headers.get("user-agent", "")[:256] if request else None,
                     request_id=getattr(request.state, "request_id", None) if request else None,
                     previous_hash=previous_hash, event_hash=digest, created_at=created)
    db.add(entry)
    db.flush()
    return entry


def available_quantity(db: Session, batch: Batch, stage: Stage) -> int:
    opening = batch.initial_quantity if batch.stage == stage else 0
    received = db.scalar(select(func.coalesce(func.sum(Transfer.received_quantity), 0)).where(
        Transfer.batch_id == batch.id, Transfer.to_stage == stage, Transfer.status == TransferStatus.CONFIRMED)) or 0
    sent = db.scalar(select(func.coalesce(func.sum(Transfer.sent_quantity), 0)).where(
        Transfer.batch_id == batch.id, Transfer.from_stage == stage,
        Transfer.status.in_([TransferStatus.SENT, TransferStatus.RECEIVED, TransferStatus.DISPUTED, TransferStatus.CONFIRMED]))) or 0
    losses = db.scalar(select(func.coalesce(func.sum(Loss.quantity), 0)).where(
        Loss.batch_id == batch.id, Loss.stage == stage, Loss.status == ReviewStatus.APPROVED)) or 0
    adjustments = db.scalar(select(func.coalesce(func.sum(BalanceAdjustment.quantity_delta), 0)).where(
        BalanceAdjustment.batch_id == batch.id, BalanceAdjustment.stage == stage, BalanceAdjustment.status == ReviewStatus.APPROVED)) or 0
    return max(0, int(opening + received - sent - losses + adjustments))


def stage_order(stage: Stage) -> int:
    return [Stage.INCUBATION, Stage.GROWING, Stage.SLAUGHTER, Stage.PACKAGING].index(stage)


def notify_stage(db: Session, stage: Stage, title: str, body: str, kind: str,
                 object_type: str | None = None, object_id: int | None = None,
                 roles=("OPERATOR", "SHIFT_LEAD")):
    from .models import Role, User
    role_values = [Role[r] for r in roles]
    query = select(User).where(User.is_active.is_(True), User.role.in_(role_values))
    if Role.MANAGER in role_values or Role.ADMIN in role_values:
        query = query.where(User.role.in_([r for r in role_values if r in (Role.MANAGER, Role.ADMIN)]))
    else:
        query = query.where(User.stage == stage)
    users = db.scalars(query).all()
    for user in users:
        db.add(Notification(user_id=user.id, title=title, body=body, kind=kind, object_type=object_type, object_id=object_id))


def to_dict(obj):
    result = {}
    for key in obj.__table__.columns.keys():
        value = getattr(obj, key)
        result[key] = value.value if hasattr(value, "value") else value.isoformat() if isinstance(value, datetime) else value
    return result
