import os
from datetime import timedelta
from sqlalchemy import select
from .database import SessionLocal
from .models import Batch, BatchStatus, Role, Stage, Transfer, TransferStatus, User, now_utc
from .security import hash_password
from .services import audit


ACCOUNTS = [
    ("inc_op1", "Оператор инкубации 1", Role.OPERATOR, Stage.INCUBATION),
    ("inc_op2", "Оператор инкубации 2", Role.OPERATOR, Stage.INCUBATION),
    ("inc_lead", "Старший смены инкубации", Role.SHIFT_LEAD, Stage.INCUBATION),
    ("grow_op1", "Оператор выращивания 1", Role.OPERATOR, Stage.GROWING),
    ("grow_op2", "Оператор выращивания 2", Role.OPERATOR, Stage.GROWING),
    ("grow_lead", "Старший смены выращивания", Role.SHIFT_LEAD, Stage.GROWING),
    ("slaughter_op1", "Оператор убоя 1", Role.OPERATOR, Stage.SLAUGHTER),
    ("slaughter_op2", "Оператор убоя 2", Role.OPERATOR, Stage.SLAUGHTER),
    ("slaughter_lead", "Старший смены убоя", Role.SHIFT_LEAD, Stage.SLAUGHTER),
    ("pack_op1", "Оператор упаковки 1", Role.OPERATOR, Stage.PACKAGING),
    ("pack_op2", "Оператор упаковки 2", Role.OPERATOR, Stage.PACKAGING),
    ("pack_lead", "Старший смены упаковки", Role.SHIFT_LEAD, Stage.PACKAGING),
    ("manager", "Руководитель производства", Role.MANAGER, None),
    ("admin", "Системный администратор", Role.ADMIN, None),
]


def seed():
    password = os.getenv("SEED_DEMO_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError("Set SEED_DEMO_PASSWORD to a temporary password of at least 12 characters")
    db = SessionLocal()
    try:
        for username, full_name, role, stage in ACCOUNTS:
            user = db.scalar(select(User).where(User.username == username))
            if not user:
                user = User(username=username, full_name=full_name, role=role, stage=stage,
                            password_hash=hash_password(password), must_change_password=True)
                db.add(user); db.flush()
                audit(db, "USER_CREATED", "user", user.id, None, new={"username": username, "seed": True})
        if db.scalar(select(Batch.id).limit(1)) is None:
            admin = db.scalar(select(User).where(User.username == "admin"))
            demo_specs = [("DEMO-2026-001", Stage.INCUBATION, 200, "TR-DEMO-001", 200, 198),
                          ("DEMO-2026-002", Stage.GROWING, 500, "TR-DEMO-002", 500, 500),
                          ("DEMO-2026-003", Stage.SLAUGHTER, 1000, "TR-DEMO-003", 1000, 995)]
            for number, stage, quantity, transfer_no, sent, received in demo_specs:
                batch=Batch(batch_number=number,stage=stage,initial_quantity=quantity,status=BatchStatus.IN_PROGRESS,created_by=admin.id,notes="Демонстрационная партия")
                db.add(batch);db.flush()
                next_stage=list(Stage)[list(Stage).index(stage)+1]
                mismatch=sent-received
                t=Transfer(transfer_number=transfer_no,batch_id=batch.id,from_stage=stage,to_stage=next_stage,sent_quantity=sent,
                           received_quantity=received,discrepancy=mismatch,status=TransferStatus.CONFIRMED,
                           sender_id=admin.id,receiver_id=admin.id,confirmed_by=admin.id,created_at=now_utc()-timedelta(days=3),
                           sent_at=now_utc()-timedelta(days=3),received_at=now_utc()-timedelta(days=3),confirmed_at=now_utc()-timedelta(days=3),
                           confirmation_comment="Демонстрационные данные" if mismatch else None)
                db.add(t);db.flush();audit(db,"TRANSFER_CONFIRMED","transfer",t.id,admin.id,new={"demo":True,"discrepancy":mismatch})
            # A pending shipment gives the receiving team a live review task in the demo.
            batch=Batch(batch_number="DEMO-2026-004",stage=Stage.GROWING,initial_quantity=120,status=BatchStatus.IN_PROGRESS,created_by=admin.id,notes="Ожидает приемки")
            db.add(batch);db.flush()
            t=Transfer(transfer_number="TR-DEMO-004",batch_id=batch.id,from_stage=Stage.GROWING,to_stage=Stage.SLAUGHTER,sent_quantity=120,status=TransferStatus.SENT,sender_id=admin.id,sent_at=now_utc(),created_at=now_utc())
            db.add(t);db.flush();audit(db,"TRANSFER_SENT","transfer",t.id,admin.id,new={"demo":True})
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    seed()
