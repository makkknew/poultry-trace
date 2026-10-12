from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .models import LossReason, Role, Stage


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordInput(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        if not any(c.islower() for c in value) or not any(c.isupper() for c in value) or not any(c.isdigit() for c in value):
            raise ValueError("Use upper and lower case letters and at least one number")
        return value


class ResetPasswordInput(BaseModel):
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        if not any(c.islower() for c in value) or not any(c.isupper() for c in value) or not any(c.isdigit() for c in value):
            raise ValueError("Use upper and lower case letters and at least one number")
        return value


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    full_name: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=12, max_length=128)
    role: Role
    stage: Stage | None = None

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        if not any(c.islower() for c in value) or not any(c.isupper() for c in value) or not any(c.isdigit() for c in value):
            raise ValueError("Use upper and lower case letters and at least one number")
        return value

    @model_validator(mode="after")
    def stage_role(self):
        if self.role in (Role.OPERATOR, Role.SHIFT_LEAD) and self.stage is None:
            raise ValueError("A production role requires a stage")
        if self.role in (Role.MANAGER, Role.ADMIN) and self.stage is not None:
            raise ValueError("Manager and admin accounts cannot be assigned to a stage")
        return self


class UserPatch(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    role: Role | None = None
    stage: Stage | None = None


class BatchCreate(BaseModel):
    batch_number: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    stage: Stage
    initial_quantity: int = Field(ge=1, le=2_000_000_000)
    notes: str | None = Field(default=None, max_length=1000)


class TransferCreate(BaseModel):
    batch_id: int = Field(gt=0)
    sent_quantity: int = Field(gt=0, le=2_000_000_000)


class TransferReceive(BaseModel):
    received_quantity: int = Field(ge=0, le=2_000_000_000)
    comment: str | None = Field(default=None, max_length=1000)


class ReviewInput(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class AdjustmentCreate(BaseModel):
    batch_id: int = Field(gt=0)
    quantity_delta: int = Field(ge=-2_000_000_000, le=2_000_000_000)
    reason: str = Field(min_length=5, max_length=1000)

    @field_validator("quantity_delta")
    @classmethod
    def nonzero_adjustment(cls, value: int) -> int:
        if value == 0:
            raise ValueError("Quantity adjustment cannot be zero")
        return value


class LossCreate(BaseModel):
    batch_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=2_000_000_000)
    reason: LossReason
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def other_requires_comment(self):
        if self.reason == LossReason.OTHER and not (self.comment or "").strip():
            raise ValueError("A comment is required when reason is OTHER")
        return self


class NotificationRead(BaseModel):
    read: bool = True


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str
    role: Role
    stage: Stage | None
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None
