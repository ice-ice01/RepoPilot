import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AnalysisResult, AuditLog, AuthBootstrap, AuthSession, EvalRun, Repository, User, Workspace
from app.db.session import get_db
from app.schemas.auth import (
    AdminPasswordResetRequest,
    AdminUserListItem,
    AuthSessionItem,
    AuthUserResponse,
    CreateUserRequest,
    Credentials,
    PasswordChangeRequest,
    RegisterRequest,
)
from app.services.auth import (
    authenticate_password, clear_cookies, get_current_user, hash_password, issue_session,
    public_user, require_bootstrap_access, utcnow, validate_origin, verify_password,
)

router = APIRouter()


@router.get("/status")
def auth_status(response: Response, db: Session = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    bootstrap = db.get(AuthBootstrap, 1)
    return {"setup_required": bootstrap is None or bootstrap.claimed_by is None,
            "registration_enabled": settings.auth_allow_registration,
            "bootstrap_token_required": bool(settings.auth_bootstrap_token) or settings.app_env.lower() == "production"}


def _create_user(db: Session, payload: RegisterRequest, role: str) -> User:
    user = User(username=payload.username, name=payload.name.strip() or payload.username,
                password_hash=hash_password(payload.password), role=role, is_active=True)
    db.add(user)
    db.flush()
    return user


@router.post("/register", response_model=AuthUserResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    validate_origin(request)
    bootstrap = db.get(AuthBootstrap, 1)
    if bootstrap is None:
        # Also supports newly created test/CLI databases that have not run startup.
        bootstrap = AuthBootstrap(id=1)
        db.add(bootstrap)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        bootstrap = db.get(AuthBootstrap, 1)
    first = bootstrap.claimed_by is None
    if first:
        require_bootstrap_access(request)
    elif not settings.auth_allow_registration:
        raise HTTPException(status_code=403, detail="自助注册未开放，请联系管理员创建账号")
    try:
        if first:
            # Preserve the original demo user's ID and token references.
            user = db.query(User).filter(User.username.is_(None)).order_by(User.created_at).first()
            if user is not None:
                user.username, user.name = payload.username, payload.name.strip() or payload.username
                user.password_hash, user.role, user.is_active = hash_password(payload.password), "owner", True
                db.flush()
            else:
                user = _create_user(db, payload, "owner")
            claimed = db.execute(update(AuthBootstrap).where(AuthBootstrap.id == 1, AuthBootstrap.claimed_by.is_(None)).values(claimed_by=user.id))
            if claimed.rowcount != 1:
                raise HTTPException(status_code=409, detail="初始化已完成，请登录")
            for model in (Repository, AnalysisResult, EvalRun):
                db.query(model).filter(model.owner_user_id.is_(None)).update({model.owner_user_id: user.id}, synchronize_session=False)
            legacy_users = db.query(User.id).filter(User.username.is_(None))
            db.query(Workspace).filter((Workspace.created_by.is_(None)) | Workspace.created_by.in_(legacy_users)).update({Workspace.created_by: user.id}, synchronize_session=False)
            db.query(AuditLog).filter((AuditLog.user_id.is_(None)) | AuditLog.user_id.in_(legacy_users)).update({AuditLog.user_id: user.id}, synchronize_session=False)
        else:
            user = _create_user(db, payload, "developer")
        issue_session(db, user, response, user_agent=request.headers.get("user-agent"))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名不可用") from exc
    except HTTPException:
        db.rollback()
        raise
    return public_user(db, user)


@router.post("/login", response_model=AuthUserResponse)
def login(payload: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):

    validate_origin(request)
    user = authenticate_password(db, request, payload.username, payload.password)
    issue_session(db, user, response, user_agent=request.headers.get("user-agent"))
    db.commit()
    return public_user(db, user)


@router.get("/me", response_model=AuthUserResponse)
def me(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return public_user(db, user)


@router.post("/logout")
def logout(request: Request, response: Response, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = db.get(AuthSession, request.state.auth_session_id)
    if session:
        db.delete(session)
    db.commit()
    clear_cookies(response)
    return {"ok": True}


@router.post("/password", response_model=AuthUserResponse)
def change_password(payload: PasswordChangeRequest, request: Request, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    user.password_hash = hash_password(payload.new_password)
    db.query(AuthSession).filter(AuthSession.user_id == user.id).delete(synchronize_session=False)
    issue_session(db, user, response, user_agent=request.headers.get("user-agent"))
    db.commit()
    return public_user(db, user)


@router.post("/users", response_model=AuthUserResponse, status_code=201)
def create_user(payload: CreateUserRequest, current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bootstrap = db.get(AuthBootstrap, 1)
    if bootstrap is None or bootstrap.claimed_by != current.id:
        raise HTTPException(status_code=403, detail="只有初始化管理员可以创建账号")
    try:
        user = _create_user(db, payload, payload.role)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名不可用") from exc
    return public_user(db, user)


def _require_admin(current: User, db: Session) -> AuthBootstrap:
    bootstrap = db.get(AuthBootstrap, 1)
    if bootstrap is None or bootstrap.claimed_by != current.id:
        raise HTTPException(status_code=403, detail="只有初始化管理员可以管理账号")
    return bootstrap


@router.get("/users", response_model=list[AdminUserListItem])
def list_users(current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(current, db)
    now = utcnow()
    session_counts: dict = dict(
        db.query(AuthSession.user_id, func.count(AuthSession.id))
        .filter(AuthSession.expires_at > now)
        .group_by(AuthSession.user_id)
        .all()
    )
    items = []
    for user in db.query(User).order_by(User.created_at).all():
        items.append(
            AdminUserListItem(
                id=user.id,
                username=user.username or "",
                name=user.name,
                role=user.role or "viewer",
                is_active=bool(user.is_active),
                active_sessions=int(session_counts.get(user.id, 0)),
            ).model_dump()
        )
    return items


@router.post("/users/{user_id}/password")
def admin_reset_password(
    user_id: uuid.UUID,
    payload: AdminPasswordResetRequest,
    request: Request,
    response: Response,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """管理员重置任意账号密码：重置后目标账号全部会话立即失效。"""
    _require_admin(current, db)
    validate_origin(request)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    target.password_hash = hash_password(payload.new_password)
    revoked = db.query(AuthSession).filter(AuthSession.user_id == target.id).delete(synchronize_session=False)
    # 若重置的是自己，给当前浏览器换发新会话，避免立刻被踢出。
    if target.id == current.id:
        issue_session(db, target, response, user_agent=request.headers.get("user-agent"))
    from app.services.permissions import write_audit_log

    write_audit_log(
        db,
        user=current,
        action="auth.admin_reset_password",
        target_type="user",
        target_id=str(target.id),
        request_json={"revoked_sessions": revoked},
    )
    db.commit()
    return {"ok": True, "username": target.username, "revoked_sessions": revoked}


@router.get("/sessions", response_model=list[AuthSessionItem])
def list_own_sessions(request: Request, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    current_session_id = getattr(request.state, "auth_session_id", None)
    now = utcnow()
    sessions = (
        db.query(AuthSession)
        .filter(AuthSession.user_id == user.id, AuthSession.expires_at > now)
        .order_by(AuthSession.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        AuthSessionItem(
            id=session.id,
            created_at=session.created_at.isoformat() if session.created_at else None,
            expires_at=session.expires_at.isoformat() if session.expires_at else None,
            user_agent=session.user_agent,
            current=session.id == current_session_id,
        )
        for session in sessions
    ]


@router.delete("/sessions/{session_id}")
def revoke_own_session(
    session_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    validate_origin(request)
    session = db.get(AuthSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    current_session_id = getattr(request.state, "auth_session_id", None)
    if session.id == current_session_id:
        raise HTTPException(status_code=400, detail="当前会话请使用退出登录")
    db.delete(session)
    db.commit()
    return {"ok": True, "revoked": str(session_id)}
