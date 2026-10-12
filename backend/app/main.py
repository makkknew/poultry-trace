import csv
import io
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from threading import Lock
from datetime import datetime, timedelta, timezone
from enum import Enum
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .config import settings
from .database import get_db
from .models import (AuditLog, BalanceAdjustment, Batch, BatchStatus, Loss, LossReason, Notification, RefreshSession,
                     ReviewStatus, Role, Stage, Transfer, TransferStatus, User, now_utc)
from .schemas import (AdjustmentCreate, BatchCreate, ChangePasswordInput, LoginInput, LossCreate, ReviewInput,
                      ResetPasswordInput, TransferCreate, TransferReceive, UserCreate, UserPatch, UserView)
from .security import decode_token, hash_password, make_access_token, make_refresh_token, token_digest, verify_password
from .services import audit, available_quantity, notify_stage, stage_order, to_dict


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                           "message": record.getMessage(), "logger": record.name}, ensure_ascii=False)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("poultry_trace")
app = FastAPI(title="Poultry Trace API", version="1.0.0", description="Production bird traceability and reconciliation API")
app.add_middleware(CORSMiddleware, allow_origins=[v.strip() for v in settings.cors_origins.split(",") if v.strip()],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
bearer = HTTPBearer(auto_error=False)
STAGES = [Stage.INCUBATION, Stage.GROWING, Stage.SLAUGHTER, Stage.PACKAGING]
_login_attempt_times: dict[str, deque[float]] = defaultdict(deque)
_login_attempt_lock = Lock()


def enforce_login_rate_limit(request: Request, username: str):
    key = f"{request.client.host if request.client else 'unknown'}:{username.casefold()}"
    now = time.monotonic()
    with _login_attempt_lock:
        attempts = _login_attempt_times[key]
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "AUTH_RATE_LIMITED")
        attempts.append(now)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    logger.info(json.dumps({"request_id": request.state.request_id, "path": request.url.path,
                            "method": request.method, "status": response.status_code,
                            "user_id": getattr(request.state, "user_id", None),
                            "duration_ms": round((time.perf_counter()-started)*1000, 2)}))
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    code = exc.detail if isinstance(exc.detail, str) else "REQUEST_ERROR"
    messages = {"AUTH_INVALID_CREDENTIALS": "Неверный логин или пароль", "AUTH_ACCOUNT_LOCKED": "Вход временно заблокирован",
                "FORBIDDEN": "Недостаточно прав", "NOT_FOUND": "Запись не найдена", "INSUFFICIENT_BALANCE": "Недостаточно доступного количества",
                "VALIDATION_ERROR": "Проверьте введенные данные"}
    return Response(content=json.dumps({"success": False, "error": {"code": code, "message": messages.get(code, code), "details": {}}}, ensure_ascii=False),
                    status_code=exc.status_code, media_type="application/json")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(part) for part in item.get("loc", ())), "message": item.get("msg", "Invalid value")} for item in exc.errors()]
    return Response(content=json.dumps({"success": False, "error": {"code": "VALIDATION_ERROR", "message": "Проверьте введенные данные", "details": details}, "request_id": request.state.request_id}, ensure_ascii=False), status_code=422, media_type="application/json")


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    logger.error(json.dumps({"request_id": request.state.request_id, "error_type": type(exc).__name__, "path": request.url.path}))
    return Response(content=json.dumps({"success": False, "error": {"code": "INTERNAL_ERROR", "message": "Внутренняя ошибка сервера", "details": {}}, "request_id": request.state.request_id}, ensure_ascii=False), status_code=500, media_type="application/json")


@app.exception_handler(IntegrityError)
async def integrity_error(request: Request, exc: IntegrityError):
    return Response(content=json.dumps({"success": False, "error": {"code": "CONFLICT", "message": "Такая запись уже существует", "details": {}}}, ensure_ascii=False), status_code=409, media_type="application/json")


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if not credentials:
        raise HTTPException(401, "AUTH_INVALID_CREDENTIALS")
    try:
        claims = decode_token(credentials.credentials)
        if claims.get("type") != "access":
            raise InvalidTokenError()
        user = db.get(User, int(claims["sub"]))
        if not user or not user.is_active:
            raise InvalidTokenError()
        request.state.user_id = user.id
        if user.must_change_password and request.url.path not in ("/auth/me", "/auth/change-password", "/auth/logout"):
            raise HTTPException(403, "PASSWORD_CHANGE_REQUIRED")
        return user
    except (InvalidTokenError, KeyError, ValueError):
        raise HTTPException(401, "AUTH_INVALID_CREDENTIALS")


def require_roles(*roles: Role):
    def dependency(user: User = Depends(current_user)):
        if user.role not in roles:
            raise HTTPException(403, "FORBIDDEN")
        return user
    return dependency


def visible_stage(user: User, stage: Stage):
    if user.role in (Role.MANAGER, Role.ADMIN):
        return
    if user.stage != stage:
        raise HTTPException(403, "FORBIDDEN")


def get_batch_or_404(db: Session, batch_id: int) -> Batch:
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "NOT_FOUND")
    return batch


def check_audit(db: Session) -> dict:
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    import hashlib
    previous = "0" * 64
    for row in rows:
        row_created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
        payload = {"action": row.action, "object_type": row.object_type, "object_id": row.object_id,
                   "actor_id": row.actor_id, "old_value": row.old_value, "new_value": row.new_value,
                   "reason": row.reason, "created_at": row_created.isoformat()}
        expected = hashlib.sha256((previous + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)).encode()).hexdigest()
        if row.previous_hash != previous or row.event_hash != expected:
            return {"valid": False, "checked": row.id, "broken_at": row.id}
        previous = row.event_hash
    return {"valid": True, "checked": len(rows), "broken_at": None}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/login")
def login(request: Request, data: LoginInput, response: Response, db: Session = Depends(get_db)):
    enforce_login_rate_limit(request, data.username)
    user = db.scalar(select(User).where(User.username == data.username))
    now = now_utc()
    locked_until = user.locked_until if user else None
    if locked_until and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if user and locked_until and locked_until > now:
        audit(db, "USER_LOGIN_FAILED", "user", user.id, user.id, request=request, reason="account_locked")
        db.commit()
        raise HTTPException(429, "AUTH_ACCOUNT_LOCKED")
    if not user or not user.is_active or not verify_password(data.password, user.password_hash):
        if user:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= settings.login_max_attempts:
                user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
                user.failed_login_attempts = 0
            audit(db, "USER_LOGIN_FAILED", "user", user.id, user.id, request=request)
        else:
            audit(db, "USER_LOGIN_FAILED", "login", data.username, None, request=request)
        db.commit()
        raise HTTPException(401, "AUTH_INVALID_CREDENTIALS")
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    request.state.user_id = user.id
    refresh = make_refresh_token(user.id)
    db.add(RefreshSession(user_id=user.id, token_hash=token_digest(refresh),
                          expires_at=now + timedelta(days=settings.refresh_token_days)))
    audit(db, "USER_LOGIN", "user", user.id, user.id, request=request)
    db.commit()
    response.set_cookie("poultry_refresh", refresh, httponly=True, secure=settings.environment == "production",
                        samesite="strict", max_age=settings.refresh_token_days * 86400, path="/api/auth")
    return {"access_token": make_access_token(user.id, user.username, user.role.value, user.stage.value if user.stage else None),
            "token_type": "bearer", "user": UserView.model_validate(user).model_dump(mode="json")}


@app.post("/auth/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get("poultry_refresh")
    if not token:
        raise HTTPException(401, "AUTH_INVALID_CREDENTIALS")
    try:
        claims = decode_token(token, refresh=True)
        session = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == token_digest(token), RefreshSession.revoked_at.is_(None)))
        user = db.get(User, int(claims["sub"]))
        expires_at = session.expires_at if session else None
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if claims.get("type") != "refresh" or not session or expires_at <= now_utc() or not user or not user.is_active:
            raise InvalidTokenError()
    except (InvalidTokenError, KeyError, ValueError):
        raise HTTPException(401, "AUTH_INVALID_CREDENTIALS")
    session.revoked_at = now_utc()
    new_refresh = make_refresh_token(user.id)
    db.add(RefreshSession(user_id=user.id, token_hash=token_digest(new_refresh), expires_at=now_utc()+timedelta(days=settings.refresh_token_days)))
    audit(db, "TOKEN_REFRESHED", "user", user.id, user.id, request=request)
    db.commit()
    response.set_cookie("poultry_refresh", new_refresh, httponly=True, secure=settings.environment == "production", samesite="strict", max_age=settings.refresh_token_days*86400, path="/api/auth")
    return {"access_token": make_access_token(user.id, user.username, user.role.value, user.stage.value if user.stage else None), "token_type": "bearer"}


@app.post("/auth/logout")
def logout(request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    token = request.cookies.get("poultry_refresh")
    if token:
        session = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == token_digest(token), RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None)))
        if session:
            session.revoked_at = now_utc()
    audit(db, "USER_LOGOUT", "user", user.id, user.id, request=request)
    db.commit()
    response.delete_cookie("poultry_refresh", path="/api/auth")
    return {"success": True}


@app.post("/auth/change-password")
def change_password(data: ChangePasswordInput, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(400, "AUTH_INVALID_CREDENTIALS")
    if verify_password(data.new_password, user.password_hash):
        raise HTTPException(422, "VALIDATION_ERROR")
    user.password_hash = hash_password(data.new_password)
    user.must_change_password = False
    audit(db, "PASSWORD_CHANGED", "user", user.id, user.id, request=request)
    db.commit()
    return {"success": True}


@app.post("/auth/forgot-password")
def forgot_password():
    # Deliberately neutral until an email delivery provider is configured.
    return {"success": True, "message": "Если учетная запись существует, инструкции будут отправлены администратором."}


@app.post("/auth/reset-password")
def reset_password():
    raise HTTPException(501, "PASSWORD_RESET_DELIVERY_NOT_CONFIGURED")


@app.get("/auth/me")
def me(user: User = Depends(current_user)):
    return UserView.model_validate(user).model_dump(mode="json")


@app.get("/stages")
def stages(user: User = Depends(current_user)):
    return [{"code": s.value, "name": {"INCUBATION":"Инкубация", "GROWING":"Взращивание", "SLAUGHTER":"Убой", "PACKAGING":"Упаковка"}[s.value], "order": i+1} for i,s in enumerate(STAGES)]


@app.get("/users")
def list_users(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.MANAGER, Role.ADMIN):
        raise HTTPException(403, "FORBIDDEN")
    return [UserView.model_validate(u).model_dump(mode="json") for u in db.scalars(select(User).order_by(User.id)).all()]


@app.post("/users", status_code=201)
def create_user(data: UserCreate, request: Request, user: User = Depends(require_roles(Role.ADMIN)), db: Session = Depends(get_db)):
    item = User(username=data.username, full_name=data.full_name, password_hash=hash_password(data.password), role=data.role,
                 stage=data.stage, must_change_password=True)
    db.add(item); db.flush(); audit(db, "USER_CREATED", "user", item.id, user.id, request=request, new={"username": item.username, "role": item.role.value, "stage": item.stage.value if item.stage else None})
    db.commit(); db.refresh(item)
    return UserView.model_validate(item).model_dump(mode="json")


@app.patch("/users/{user_id}")
def patch_user(user_id: int, data: UserPatch, request: Request, user: User = Depends(require_roles(Role.ADMIN)), db: Session = Depends(get_db)):
    item = db.get(User, user_id)
    if not item: raise HTTPException(404, "NOT_FOUND")
    old = {"full_name": item.full_name, "role": item.role.value, "stage": item.stage.value if item.stage else None}
    values = data.model_dump(exclude_unset=True)
    role = values.get("role", item.role); stage = values.get("stage", item.stage)
    if role in (Role.OPERATOR, Role.SHIFT_LEAD) and stage is None or role in (Role.MANAGER, Role.ADMIN) and stage is not None:
        raise HTTPException(422, "VALIDATION_ERROR")
    if user_id == user.id and role != Role.ADMIN: raise HTTPException(400, "CANNOT_REMOVE_OWN_ADMIN")
    for key, value in values.items(): setattr(item, key, value)
    audit(db, "USER_UPDATED", "user", item.id, user.id, request=request, old=old, new=values)
    db.commit(); db.refresh(item)
    return UserView.model_validate(item).model_dump(mode="json")


@app.post("/users/{user_id}/disable")
def disable_user(user_id: int, request: Request, user: User = Depends(require_roles(Role.ADMIN)), db: Session = Depends(get_db)):
    item = db.get(User, user_id)
    if not item: raise HTTPException(404, "NOT_FOUND")
    if item.id == user.id: raise HTTPException(400, "CANNOT_DISABLE_SELF")
    item.is_active = False
    audit(db, "USER_BLOCKED", "user", item.id, user.id, request=request); db.commit()
    return {"success": True}


@app.post("/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, data: ResetPasswordInput, request: Request, user: User = Depends(require_roles(Role.ADMIN)), db: Session = Depends(get_db)):
    item = db.get(User, user_id)
    if not item: raise HTTPException(404, "NOT_FOUND")
    item.password_hash = hash_password(data.new_password)
    item.must_change_password = True
    item.failed_login_attempts = 0
    item.locked_until = None
    for session in db.scalars(select(RefreshSession).where(RefreshSession.user_id == item.id, RefreshSession.revoked_at.is_(None))).all():
        session.revoked_at = now_utc()
    audit(db, "PASSWORD_RESET_BY_ADMIN", "user", item.id, user.id, request=request)
    db.commit()
    return {"success": True, "must_change_password": True}


@app.get("/batches")
def list_batches(user: User = Depends(current_user), db: Session = Depends(get_db), q: str | None = None, status: BatchStatus | None = None, limit: int = 50, offset: int = 0):
    query = select(Batch)
    if user.role not in (Role.MANAGER, Role.ADMIN):
        # Operators see batches that have touched their stage, and only batches they originated.
        query = query.where((Batch.stage == user.stage) | (Batch.id.in_(select(Transfer.batch_id).where((Transfer.from_stage == user.stage) | (Transfer.to_stage == user.stage)))))
    if q: query = query.where(Batch.batch_number.ilike(f"%{q[:80]}%"))
    if status: query = query.where(Batch.status == status)
    rows = db.scalars(query.order_by(Batch.created_at.desc()).limit(min(limit,100)).offset(offset)).all()
    return [{**to_dict(b), "available_quantity": available_quantity(db,b,user.stage) if user.stage else None} for b in rows]


@app.post("/batches", status_code=201)
def create_batch(data: BatchCreate, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.OPERATOR, Role.SHIFT_LEAD, Role.ADMIN): raise HTTPException(403,"FORBIDDEN")
    if user.role != Role.ADMIN and user.stage != data.stage: raise HTTPException(403,"FORBIDDEN")
    item = Batch(batch_number=data.batch_number, stage=data.stage, initial_quantity=data.initial_quantity,
                 status=BatchStatus.IN_PROGRESS, notes=data.notes, created_by=user.id)
    db.add(item); db.flush(); audit(db,"BATCH_CREATED","batch",item.id,user.id,request=request,new={"batch_number":item.batch_number,"quantity":item.initial_quantity,"stage":item.stage.value}); db.commit(); db.refresh(item)
    return {**to_dict(item), "available_quantity": item.initial_quantity}


@app.get("/batches/{batch_id}")
def batch_detail(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = get_batch_or_404(db,batch_id)
    if user.role not in (Role.MANAGER,Role.ADMIN):
        relevant = item.stage == user.stage or db.scalar(select(Transfer.id).where(Transfer.batch_id==item.id, (Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage)).limit(1))
        if not relevant: raise HTTPException(403,"FORBIDDEN")
    transfer_query=select(Transfer).where(Transfer.batch_id==item.id)
    loss_query=select(Loss).where(Loss.batch_id==item.id)
    if user.role not in (Role.MANAGER,Role.ADMIN):
        transfer_query=transfer_query.where((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage))
        loss_query=loss_query.where(Loss.stage==user.stage)
    transfers = db.scalars(transfer_query.order_by(Transfer.created_at)).all()
    losses = db.scalars(loss_query.order_by(Loss.created_at)).all()
    return {**to_dict(item), "transfers":[to_dict(t) for t in transfers], "losses":[to_dict(l) for l in losses]}


@app.get("/transfers")
def list_transfers(user: User = Depends(current_user), db: Session = Depends(get_db), status: TransferStatus | None = None, q: str | None = None, limit: int = 50, offset: int = 0):
    query = select(Transfer)
    if user.role not in (Role.MANAGER, Role.ADMIN): query = query.where((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage))
    if status: query=query.where(Transfer.status==status)
    if q: query=query.where(Transfer.transfer_number.ilike(f"%{q[:80]}%"))
    return [to_dict(t) for t in db.scalars(query.order_by(Transfer.created_at.desc()).limit(min(limit,100)).offset(offset)).all()]


@app.post("/transfers", status_code=201)
def create_transfer(data: TransferCreate, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.OPERATOR,Role.SHIFT_LEAD,Role.ADMIN): raise HTTPException(403,"FORBIDDEN")
    batch=get_batch_or_404(db,data.batch_id)
    source = user.stage if user.role != Role.ADMIN else batch.stage
    if source is None: raise HTTPException(422,"INVALID_STAGE")
    visible_stage(user, source)
    if stage_order(source)>=len(STAGES)-1: raise HTTPException(422,"INVALID_STAGE")
    if available_quantity(db,batch,source)<data.sent_quantity:
        raise HTTPException(409,"INSUFFICIENT_BALANCE", headers={"X-Available-Quantity":str(available_quantity(db,batch,source))})
    number=f"TR-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    item=Transfer(transfer_number=number,batch_id=batch.id,from_stage=source,to_stage=STAGES[stage_order(source)+1],sent_quantity=data.sent_quantity,status=TransferStatus.DRAFT,sender_id=user.id)
    db.add(item); db.flush(); audit(db,"TRANSFER_CREATED","transfer",item.id,user.id,request=request,new={"quantity":item.sent_quantity,"from":source.value,"to":item.to_stage.value}); db.commit(); db.refresh(item)
    return to_dict(item)


@app.post("/transfers/{transfer_id}/send")
def send_transfer(transfer_id: int, request: Request, user: User=Depends(current_user), db: Session=Depends(get_db)):
    t=db.get(Transfer,transfer_id)
    if not t: raise HTTPException(404,"NOT_FOUND")
    visible_stage(user,t.from_stage)
    if user.role not in (Role.OPERATOR,Role.SHIFT_LEAD,Role.ADMIN) or t.sender_id!=user.id or t.status!=TransferStatus.DRAFT: raise HTTPException(409,"INVALID_TRANSFER")
    batch=get_batch_or_404(db,t.batch_id)
    if available_quantity(db,batch,t.from_stage)<t.sent_quantity: raise HTTPException(409,"INSUFFICIENT_BALANCE")
    t.status=TransferStatus.SENT;t.sent_at=now_utc()
    batch.status=BatchStatus.TRANSFERRED if available_quantity(db,batch,t.from_stage)==0 else BatchStatus.IN_PROGRESS
    notify_stage(db,t.to_stage,"Ожидается передача",f"Партия {batch.batch_number}: {t.sent_quantity} голов","TRANSFER", "transfer", t.id)
    audit(db,"TRANSFER_SENT","transfer",t.id,user.id,request=request,new={"status":t.status.value});db.commit();return to_dict(t)


@app.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id:int,data:TransferReceive,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Transfer,transfer_id)
    if not t:raise HTTPException(404,"NOT_FOUND")
    visible_stage(user,t.to_stage)
    if t.status!=TransferStatus.SENT:raise HTTPException(409,"TRANSFER_ALREADY_RECEIVED")
    if user.role not in (Role.OPERATOR,Role.SHIFT_LEAD,Role.ADMIN):raise HTTPException(403,"FORBIDDEN")
    if data.received_quantity>t.sent_quantity:raise HTTPException(422,"INVALID_QUANTITY")
    if t.sent_quantity!=data.received_quantity and not (data.comment or "").strip():raise HTTPException(422,"DISCREPANCY_COMMENT_REQUIRED")
    t.received_quantity=data.received_quantity;t.discrepancy=t.sent_quantity-data.received_quantity;t.receiver_id=user.id;t.received_at=now_utc();t.confirmation_comment=data.comment
    t.status=TransferStatus.DISPUTED if t.discrepancy else TransferStatus.RECEIVED
    batch=get_batch_or_404(db,t.batch_id);batch.status=BatchStatus.DISPUTED if t.discrepancy else BatchStatus.RECEIVED
    if t.discrepancy:
        notify_stage(db,t.to_stage,"Расхождение требует подтверждения",f"{t.transfer_number}: расхождение {t.discrepancy} голов","DISCREPANCY","transfer",t.id,roles=("SHIFT_LEAD",))
        notify_stage(db,t.to_stage,"Получена партия с расхождением",f"{t.transfer_number}: {t.discrepancy} голов","DISCREPANCY","transfer",t.id,roles=("MANAGER",))
    audit(db,"TRANSFER_RECEIVED","transfer",t.id,user.id,request=request,new={"received_quantity":t.received_quantity,"discrepancy":t.discrepancy},reason=data.comment);db.commit();return to_dict(t)


@app.post("/transfers/{transfer_id}/confirm")
def confirm_transfer(transfer_id:int,data:ReviewInput,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Transfer,transfer_id)
    if not t:raise HTTPException(404,"NOT_FOUND")
    visible_stage(user,t.to_stage)
    if user.role not in (Role.SHIFT_LEAD,Role.ADMIN) or user.role==Role.SHIFT_LEAD and user.stage!=t.to_stage:raise HTTPException(403,"FORBIDDEN")
    if t.status not in (TransferStatus.RECEIVED,TransferStatus.DISPUTED):raise HTTPException(409,"TRANSFER_ALREADY_CONFIRMED")
    if t.discrepancy and not (data.reason or t.confirmation_comment or "").strip():raise HTTPException(422,"DISCREPANCY_COMMENT_REQUIRED")
    t.status=TransferStatus.CONFIRMED;t.confirmed_by=user.id;t.confirmed_at=now_utc()
    if data.reason:t.confirmation_comment=data.reason
    batch=get_batch_or_404(db,t.batch_id);batch.status=BatchStatus.IN_PROGRESS
    audit(db,"TRANSFER_CONFIRMED","transfer",t.id,user.id,request=request,new={"status":t.status.value},reason=data.reason);db.commit();return to_dict(t)


@app.post("/transfers/{transfer_id}/reject")
def reject_transfer(transfer_id:int,data:ReviewInput,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Transfer,transfer_id)
    if not t:raise HTTPException(404,"NOT_FOUND")
    visible_stage(user,t.to_stage)
    if user.role not in (Role.SHIFT_LEAD,Role.ADMIN) or user.role==Role.SHIFT_LEAD and user.stage!=t.to_stage:raise HTTPException(403,"FORBIDDEN")
    if t.status not in (TransferStatus.RECEIVED,TransferStatus.DISPUTED) or not (data.reason or "").strip():raise HTTPException(422,"VALIDATION_ERROR")
    t.status=TransferStatus.SENT;t.received_quantity=None;t.discrepancy=None;t.receiver_id=None;t.received_at=None;t.rejection_reason=data.reason
    audit(db,"TRANSFER_REJECTED","transfer",t.id,user.id,request=request,reason=data.reason);db.commit();return to_dict(t)


@app.get("/losses")
def list_losses(user:User=Depends(current_user),db:Session=Depends(get_db),status:ReviewStatus|None=None,limit:int=50,offset:int=0):
    query=select(Loss)
    if user.role not in (Role.MANAGER,Role.ADMIN):query=query.where(Loss.stage==user.stage)
    if status:query=query.where(Loss.status==status)
    return [to_dict(x) for x in db.scalars(query.order_by(Loss.created_at.desc()).limit(min(limit,100)).offset(offset)).all()]


@app.post("/losses",status_code=201)
def create_loss(data:LossCreate,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    if user.role not in (Role.OPERATOR,Role.SHIFT_LEAD,Role.ADMIN):raise HTTPException(403,"FORBIDDEN")
    batch=get_batch_or_404(db,data.batch_id);stage=user.stage if user.role!=Role.ADMIN else batch.stage
    if stage is None:raise HTTPException(422,"INVALID_STAGE")
    visible_stage(user,stage)
    if available_quantity(db,batch,stage)<data.quantity:raise HTTPException(409,"INSUFFICIENT_BALANCE")
    loss=Loss(batch_id=batch.id,stage=stage,quantity=data.quantity,reason=data.reason,comment=data.comment,created_by=user.id,
              status=ReviewStatus.PENDING if data.quantity>=settings.loss_approval_threshold else ReviewStatus.APPROVED,
              approved_by=None if data.quantity>=settings.loss_approval_threshold else user.id,
              approved_at=None if data.quantity>=settings.loss_approval_threshold else now_utc())
    db.add(loss);db.flush();audit(db,"LOSS_CREATED","loss",loss.id,user.id,request=request,new={"quantity":loss.quantity,"reason":loss.reason.value})
    if loss.status==ReviewStatus.PENDING:notify_stage(db,stage,"Потеря ожидает подтверждения",f"Партия {batch.batch_number}: {loss.quantity} голов","LOSS","loss",loss.id,roles=("SHIFT_LEAD",))
    db.commit();db.refresh(loss);return to_dict(loss)


@app.post("/losses/{loss_id}/approve")
def approve_loss(loss_id:int,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    loss=db.get(Loss,loss_id)
    if not loss:raise HTTPException(404,"NOT_FOUND")
    visible_stage(user,loss.stage)
    if user.role not in (Role.SHIFT_LEAD,Role.ADMIN) or user.role==Role.SHIFT_LEAD and user.stage!=loss.stage:raise HTTPException(403,"FORBIDDEN")
    if loss.created_by==user.id:raise HTTPException(403,"SEPARATION_OF_DUTIES_REQUIRED")
    if loss.status!=ReviewStatus.PENDING:raise HTTPException(409,"INVALID_TRANSFER")
    if available_quantity(db,get_batch_or_404(db,loss.batch_id),loss.stage)<loss.quantity:raise HTTPException(409,"INSUFFICIENT_BALANCE")
    loss.status=ReviewStatus.APPROVED;loss.approved_by=user.id;loss.approved_at=now_utc()
    audit(db,"LOSS_APPROVED","loss",loss.id,user.id,request=request,new={"status":loss.status.value});db.commit();return to_dict(loss)


@app.post("/losses/{loss_id}/reject")
def reject_loss(loss_id:int,data:ReviewInput,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    loss=db.get(Loss,loss_id)
    if not loss:raise HTTPException(404,"NOT_FOUND")
    visible_stage(user,loss.stage)
    if user.role not in (Role.SHIFT_LEAD,Role.ADMIN) or user.role==Role.SHIFT_LEAD and user.stage!=loss.stage:raise HTTPException(403,"FORBIDDEN")
    if loss.status!=ReviewStatus.PENDING or not (data.reason or "").strip():raise HTTPException(422,"VALIDATION_ERROR")
    loss.status=ReviewStatus.REJECTED;audit(db,"LOSS_REJECTED","loss",loss.id,user.id,request=request,reason=data.reason);db.commit();return to_dict(loss)


@app.get("/balance-adjustments")
def list_adjustments(user:User=Depends(current_user),db:Session=Depends(get_db),status:ReviewStatus|None=None):
    query=select(BalanceAdjustment)
    if user.role not in (Role.MANAGER,Role.ADMIN):query=query.where(BalanceAdjustment.stage==user.stage)
    if status:query=query.where(BalanceAdjustment.status==status)
    return [to_dict(row) for row in db.scalars(query.order_by(BalanceAdjustment.created_at.desc()).limit(100)).all()]


@app.post("/balance-adjustments",status_code=201)
def create_adjustment(data:AdjustmentCreate,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    if user.role not in (Role.OPERATOR,Role.SHIFT_LEAD,Role.ADMIN):raise HTTPException(403,"FORBIDDEN")
    batch=get_batch_or_404(db,data.batch_id);stage=user.stage if user.role!=Role.ADMIN else batch.stage
    if stage is None:raise HTTPException(422,"INVALID_STAGE")
    visible_stage(user,stage)
    if data.quantity_delta<0 and available_quantity(db,batch,stage)<abs(data.quantity_delta):raise HTTPException(409,"INSUFFICIENT_BALANCE")
    row=BalanceAdjustment(batch_id=batch.id,stage=stage,quantity_delta=data.quantity_delta,reason=data.reason,status=ReviewStatus.PENDING,created_by=user.id)
    db.add(row);db.flush();notify_stage(db,stage,"Корректировка ожидает подтверждения",f"Партия {batch.batch_number}: {data.quantity_delta:+} голов","ADJUSTMENT","adjustment",row.id,roles=("SHIFT_LEAD",))
    audit(db,"BALANCE_CORRECTION_CREATED","balance_adjustment",row.id,user.id,request=request,new={"quantity_delta":row.quantity_delta},reason=row.reason);db.commit();db.refresh(row);return to_dict(row)


@app.post("/balance-adjustments/{adjustment_id}/approve")
def approve_adjustment(adjustment_id:int,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    row=db.get(BalanceAdjustment,adjustment_id)
    if not row:raise HTTPException(404,"NOT_FOUND")
    visible_stage(user,row.stage)
    if user.role not in (Role.SHIFT_LEAD,Role.ADMIN) or user.role==Role.SHIFT_LEAD and user.stage!=row.stage:raise HTTPException(403,"FORBIDDEN")
    if row.created_by==user.id:raise HTTPException(403,"SEPARATION_OF_DUTIES_REQUIRED")
    if row.status!=ReviewStatus.PENDING:raise HTTPException(409,"INVALID_TRANSFER")
    if row.quantity_delta<0 and available_quantity(db,get_batch_or_404(db,row.batch_id),row.stage)<abs(row.quantity_delta):raise HTTPException(409,"INSUFFICIENT_BALANCE")
    row.status=ReviewStatus.APPROVED;row.approved_by=user.id;row.approved_at=now_utc()
    audit(db,"BALANCE_CORRECTED","balance_adjustment",row.id,user.id,request=request,new={"quantity_delta":row.quantity_delta},reason=row.reason);db.commit();return to_dict(row)


@app.post("/balance-adjustments/{adjustment_id}/reject")
def reject_adjustment(adjustment_id:int,data:ReviewInput,request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    row=db.get(BalanceAdjustment,adjustment_id)
    if not row:raise HTTPException(404,"NOT_FOUND")
    visible_stage(user,row.stage)
    if user.role not in (Role.SHIFT_LEAD,Role.ADMIN) or user.role==Role.SHIFT_LEAD and user.stage!=row.stage:raise HTTPException(403,"FORBIDDEN")
    if row.status!=ReviewStatus.PENDING or not (data.reason or "").strip():raise HTTPException(422,"VALIDATION_ERROR")
    row.status=ReviewStatus.REJECTED;audit(db,"BALANCE_CORRECTION_REJECTED","balance_adjustment",row.id,user.id,request=request,reason=data.reason);db.commit();return to_dict(row)


@app.get("/dashboard")
def dashboard(user:User=Depends(current_user),db:Session=Depends(get_db)):
    stages=STAGES if user.role in (Role.MANAGER,Role.ADMIN) else [user.stage]
    summary=[]
    for stage in stages:
        batches=db.scalars(select(Batch)).all()
        received=db.scalar(select(func.coalesce(func.sum(Transfer.received_quantity),0)).where(Transfer.to_stage==stage,Transfer.status==TransferStatus.CONFIRMED)) or 0
        sent=db.scalar(select(func.coalesce(func.sum(Transfer.sent_quantity),0)).where(Transfer.from_stage==stage,Transfer.status.in_([TransferStatus.SENT,TransferStatus.RECEIVED,TransferStatus.DISPUTED,TransferStatus.CONFIRMED]))) or 0
        losses=db.scalar(select(func.coalesce(func.sum(Loss.quantity),0)).where(Loss.stage==stage,Loss.status==ReviewStatus.APPROVED)) or 0
        adjustments=db.scalar(select(func.coalesce(func.sum(BalanceAdjustment.quantity_delta),0)).where(BalanceAdjustment.stage==stage,BalanceAdjustment.status==ReviewStatus.APPROVED)) or 0
        opening=sum(b.initial_quantity for b in batches if b.stage==stage)
        summary.append({"stage":stage.value,"opening":opening,"received":int(received),"transferred":int(sent),"losses":int(losses),"available":int(opening+received-sent-losses+adjustments)})
    discrepancy_query=select(Transfer).where(Transfer.discrepancy>0)
    if user.role not in (Role.MANAGER,Role.ADMIN):discrepancy_query=discrepancy_query.where((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage))
    first=db.scalar(discrepancy_query.order_by(Transfer.created_at.asc()).limit(1))
    discrepancies=db.scalars(discrepancy_query.order_by(Transfer.created_at.desc()).limit(10)).all()
    total_discrepancies=db.scalar(select(func.coalesce(func.sum(Transfer.discrepancy),0)).where(Transfer.discrepancy>0,Transfer.status==TransferStatus.CONFIRMED, *((((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage)),) if user.role not in (Role.MANAGER,Role.ADMIN) else ()))) or 0
    active_query=select(func.count(Batch.id)).where(Batch.status.in_([BatchStatus.IN_PROGRESS,BatchStatus.DISPUTED]))
    if user.role not in (Role.MANAGER,Role.ADMIN):active_query=active_query.where((Batch.stage==user.stage)|(Batch.id.in_(select(Transfer.batch_id).where((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage)))))
    return {"stages":summary,"active_batches":db.scalar(active_query) or 0,
            "total_discrepancies":int(total_discrepancies),"first_discrepancy":({"stage":first.to_stage.value,"quantity":first.discrepancy,"transfer_number":first.transfer_number,"created_at":first.created_at.isoformat()} if first else None),
            "recent_discrepancies":[to_dict(t) for t in discrepancies]}


@app.get("/analytics")
def analytics(user:User=Depends(current_user),db:Session=Depends(get_db),stage:Stage|None=None,from_date:datetime|None=None,to_date:datetime|None=None,batch_id:int|None=None,operator_id:int|None=None):
    if user.role not in (Role.MANAGER,Role.ADMIN) and stage not in (None,user.stage):raise HTTPException(403,"FORBIDDEN")
    query=select(Transfer)
    stage_filter = stage if stage is not None else user.stage if user.role not in (Role.MANAGER, Role.ADMIN) else None
    if stage_filter is not None:query=query.where(Transfer.to_stage==stage_filter)
    if from_date:query=query.where(Transfer.created_at>=from_date)
    if to_date:query=query.where(Transfer.created_at<=to_date)
    if batch_id:query=query.where(Transfer.batch_id==batch_id)
    if operator_id:query=query.where((Transfer.sender_id==operator_id)|(Transfer.receiver_id==operator_id))
    rows=db.scalars(query).all()
    completed=[t for t in rows if t.status==TransferStatus.CONFIRMED]
    loss_query=select(Loss).where(Loss.status==ReviewStatus.APPROVED)
    if user.role not in (Role.MANAGER,Role.ADMIN):loss_query=loss_query.where(Loss.stage==user.stage)
    loss_rows=db.scalars(loss_query).all()
    discrepancies=[abs(t.discrepancy or 0) for t in completed if t.discrepancy]
    sent=sum(t.sent_quantity for t in completed)
    problem_query=select(Transfer.batch_id,func.sum(Transfer.discrepancy)).where(Transfer.discrepancy>0,Transfer.status==TransferStatus.CONFIRMED)
    reason_query=select(Loss.reason,func.sum(Loss.quantity)).where(Loss.status==ReviewStatus.APPROVED)
    if user.role not in (Role.MANAGER,Role.ADMIN):
        problem_query=problem_query.where((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage))
        reason_query=reason_query.where(Loss.stage==user.stage)
    if stage_filter is not None:
        problem_query=problem_query.where(Transfer.to_stage==stage_filter)
        reason_query=reason_query.where(Loss.stage==stage_filter)
    if from_date: problem_query=problem_query.where(Transfer.created_at>=from_date); reason_query=reason_query.where(Loss.created_at>=from_date)
    if to_date: problem_query=problem_query.where(Transfer.created_at<=to_date); reason_query=reason_query.where(Loss.created_at<=to_date)
    if batch_id: problem_query=problem_query.where(Transfer.batch_id==batch_id); reason_query=reason_query.where(Loss.batch_id==batch_id)
    if operator_id: problem_query=problem_query.where((Transfer.sender_id==operator_id)|(Transfer.receiver_id==operator_id)); reason_query=reason_query.where(Loss.created_by==operator_id)
    return {"operation_count":len(rows),"discrepancy_count":len(discrepancies),"average_discrepancy":round(sum(discrepancies)/len(discrepancies),2) if discrepancies else 0,
            "loss_quantity":sum(l.quantity for l in loss_rows),"loss_percent":round(sum(l.quantity for l in loss_rows)/sent*100,2) if sent else 0,
            "problem_batches":[{"batch_id":bid,"discrepancy":qty} for bid,qty in db.execute(problem_query.group_by(Transfer.batch_id).order_by(func.sum(Transfer.discrepancy).desc()).limit(10)).all()],
            "loss_reasons":[{"reason":reason.value if isinstance(reason,Enum) else reason,"quantity":qty} for reason,qty in db.execute(reason_query.group_by(Loss.reason)).all()]}


@app.get("/alerts")
def alerts(user:User=Depends(current_user),db:Session=Depends(get_db)):
    query=select(Transfer).where(Transfer.discrepancy>0)
    if user.role not in (Role.MANAGER,Role.ADMIN):query=query.where((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage))
    rows=db.scalars(query.order_by(Transfer.created_at.desc())).all()
    counts={stage:sum(1 for t in rows if t.to_stage==stage) for stage in STAGES}
    result=[{"severity":"LOW" if t.discrepancy<3 else "MEDIUM" if t.discrepancy<10 else "HIGH","message":f"Расхождение {t.discrepancy} голов требует проверки","transfer":to_dict(t)} for t in rows]
    for stage,count in counts.items():
        if count>=3:result.append({"severity":"CRITICAL","message":f"Повторяющиеся расхождения на этапе {stage.value}: {count}","stage":stage.value})
    return result


@app.get("/notifications")
def notifications(user:User=Depends(current_user),db:Session=Depends(get_db),unread_only:bool=False):
    query=select(Notification).where(Notification.user_id==user.id)
    if unread_only:query=query.where(Notification.read_at.is_(None))
    return [to_dict(n) for n in db.scalars(query.order_by(Notification.created_at.desc()).limit(100)).all()]


@app.post("/notifications/{notification_id}/read")
def mark_read(notification_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    item=db.get(Notification,notification_id)
    if not item or item.user_id!=user.id:raise HTTPException(404,"NOT_FOUND")
    item.read_at=now_utc();db.commit();return {"success":True}


@app.get("/audit-logs")
def audit_logs(user:User=Depends(require_roles(Role.MANAGER,Role.ADMIN)),db:Session=Depends(get_db),limit:int=100,offset:int=0):
    rows=db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit,500)).offset(offset)).all()
    return [to_dict(a) for a in rows]


@app.get("/audit-logs/verify")
def audit_verify(user:User=Depends(require_roles(Role.ADMIN)),db:Session=Depends(get_db)):
    return check_audit(db)


@app.get("/export/{report_type}")
def export_report(report_type:str,format:str="csv",user:User=Depends(require_roles(Role.MANAGER,Role.ADMIN)),db:Session=Depends(get_db)):
    if report_type not in ("batches","transfers","losses","discrepancies","audit"):raise HTTPException(404,"NOT_FOUND")
    model={"batches":Batch,"transfers":Transfer,"losses":Loss,"discrepancies":Transfer,"audit":AuditLog}[report_type]
    query=select(model)
    if report_type=="discrepancies":query=query.where(Transfer.discrepancy>0)
    rows=db.scalars(query.order_by(model.id)).all()
    records=[to_dict(x) for x in rows]
    if format=="csv":
        output=io.StringIO();headers=list(records[0]) if records else [c.name for c in model.__table__.columns]
        writer=csv.DictWriter(output,fieldnames=headers);writer.writeheader();writer.writerows(records)
        return StreamingResponse(iter([output.getvalue().encode("utf-8-sig")]),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":f"attachment; filename={report_type}.csv"})
    if format=="xlsx":
        from openpyxl import Workbook
        output=io.BytesIO();book=Workbook();sheet=book.active;sheet.title=report_type.title()
        headers=list(records[0]) if records else [c.name for c in model.__table__.columns];sheet.append(headers)
        for item in records:sheet.append([str(item.get(h) or "") for h in headers])
        book.save(output);output.seek(0)
        return StreamingResponse(iter([output.getvalue()]),media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f"attachment; filename={report_type}.xlsx"})
    if format=="pdf":
        from reportlab.lib.pagesizes import landscape, A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        output=io.BytesIO();doc=SimpleDocTemplate(output,pagesize=landscape(A4));headers=list(records[0]) if records else [c.name for c in model.__table__.columns]
        data=[[Paragraph(str(h),getSampleStyleSheet()["BodyText"]) for h in headers]]+[[Paragraph(str(row.get(h) or ""),getSampleStyleSheet()["BodyText"]) for h in headers] for row in records]
        table=Table(data,repeatRows=1);table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#dbe7dd")),("GRID",(0,0),(-1,-1),.25,colors.grey),("VALIGN",(0,0),(-1,-1),"TOP")]));doc.build([Paragraph(f"Poultry Trace — {report_type}",getSampleStyleSheet()["Title"]),table]);output.seek(0)
        return StreamingResponse(iter([output.getvalue()]),media_type="application/pdf",headers={"Content-Disposition":f"attachment; filename={report_type}.pdf"})
    raise HTTPException(422,"UNSUPPORTED_EXPORT_FORMAT")


@app.get("/search")
def search(q:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    term=f"%{q[:80]}%";batch_query=select(Batch).where(Batch.batch_number.ilike(term)).limit(20)
    if user.role not in (Role.MANAGER,Role.ADMIN):batch_query=batch_query.where((Batch.stage==user.stage)|(Batch.id.in_(select(Transfer.batch_id).where((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage)))))
    batches=db.scalars(batch_query).all();transfer_query=select(Transfer).where(Transfer.transfer_number.ilike(term)).limit(20)
    if user.role not in (Role.MANAGER,Role.ADMIN):transfer_query=transfer_query.where((Transfer.from_stage==user.stage)|(Transfer.to_stage==user.stage))
    return {"batches":[to_dict(b) for b in batches],"transfers":[to_dict(t) for t in db.scalars(transfer_query).all()]}

