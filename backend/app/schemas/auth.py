from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.lower()


class RegisterRequest(Credentials):
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(default="", max_length=200)


class CreateUserRequest(RegisterRequest):
    role: Literal["owner", "maintainer", "developer", "viewer"] = "developer"


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class AdminPasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=12, max_length=128)


class AuthUserResponse(BaseModel):
    id: UUID
    username: str
    name: str
    role: str
    is_admin: bool = False


class AdminUserListItem(BaseModel):
    id: UUID
    username: str
    name: str
    role: str
    is_active: bool
    active_sessions: int = 0


class AuthSessionItem(BaseModel):
    id: UUID
    created_at: str | None = None
    expires_at: str | None = None
    user_agent: str | None = None
    current: bool = False
