import hashlib
import hmac
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AuthBootstrap, AuthSession, AuthThrottle, User
from app.db.session import get_db
from app.services.data_access import bind_user

SESSION_COOKIE = "devflow_session"
CSRF_COOKIE = "devflow_csrf"
# 迭代数可通过 PBKDF2_ITERATIONS 覆盖（测试环境降到 2000 提速）；verify 同步放宽下限。
MIN_PASSWORD_ITERATIONS = 1_000
MAX_PASSWORD_ITERATIONS = 2_000_000


def _password_iterations() -> int:
    return max(MIN_PASSWORD_ITERATIONS, min(int(settings.pbkdf2_iterations), MAX_PASSWORD_ITERATIONS))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = _password_iterations()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, iterations, salt, expected = (encoded or "").split("$")
        # 下限与测试覆盖联动：哈希只由服务端生成，放宽下限不会引入外部弱哈希。
        if algorithm != "pbkdf2_sha256" or not MIN_PASSWORD_ITERATIONS <= int(iterations) <= MAX_PASSWORD_ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    allowed = {value.strip().rstrip("/") for value in settings.cors_origins.split(",") if value.strip()}
    allowed.add(str(request.base_url).rstrip("/"))
    if origin and origin.rstrip("/") not in allowed:
        raise HTTPException(status_code=403, detail="请求来源不受信任")
    if not origin and request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="不允许跨站请求")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    authorization = request.headers.get("authorization")
    from_cookie = authorization is None
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            token = ""
    else:
        token = request.cookies.get(SESSION_COOKIE, "")
    if not token or len(token) > 512:
        raise HTTPException(status_code=401, detail="请先登录", headers={"WWW-Authenticate": "Bearer"})
    session = db.query(AuthSession).filter(AuthSession.token_hash == token_hash(token)).one_or_none()
    if session is None or aware(session.expires_at) <= utcnow():
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    user = db.get(User, session.user_id)
    if user is None or not user.is_active or not user.password_hash:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    if from_cookie and request.method not in {"GET", "HEAD", "OPTIONS"}:
        validate_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not hmac.compare_digest(token_hash(csrf), session.csrf_hash):
            raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面后重试")
    bind_user(db, user)
    request.state.auth_session_id = session.id
    return user


def issue_session(db: Session, user: User, response: Response, user_agent: str | None = None) -> None:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    max_age = settings.auth_session_hours * 3600
    db.add(AuthSession(
        user_id=user.id,
        token_hash=token_hash(token),
        csrf_hash=token_hash(csrf),
        expires_at=utcnow() + timedelta(seconds=max_age),
        # 记录登录端 User Agent，供“会话管理”页识别设备；截断避免超长头部入库。
        user_agent=(user_agent or "")[:300] or None,
    ))
    secure = settings.auth_cookie_secure or settings.app_env.lower() == "production"
    options = {"max_age": max_age, "secure": secure, "samesite": "lax", "path": "/"}
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **options)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **options)
    response.headers["Cache-Control"] = "no-store"


def clear_cookies(response: Response) -> None:
    secure = settings.auth_cookie_secure or settings.app_env.lower() == "production"
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, samesite="lax")


def public_user(db: Session, user: User) -> dict:
    bootstrap = db.get(AuthBootstrap, 1)
    return {"id": user.id, "username": user.username or "", "name": user.name, "role": user.role, "is_admin": bool(bootstrap and bootstrap.claimed_by == user.id)}


def require_bootstrap_access(request: Request) -> None:
    if settings.auth_bootstrap_token:
        provided = request.headers.get("x-bootstrap-token", "")
        if not hmac.compare_digest(provided, settings.auth_bootstrap_token):
            raise HTTPException(status_code=403, detail="首次初始化需要正确的初始化密钥")
        return
    try:
        local = ipaddress.ip_address(request.client.host if request.client else "").is_loopback
    except ValueError:
        local = False
    if settings.app_env.lower() == "production" or not local:
        raise HTTPException(status_code=403, detail="请从本机初始化，或配置 AUTH_BOOTSTRAP_TOKEN")


# 两个维度限流： 15分钟内最多5次失败，同一个IP最多30次失败
def _throttle_keys(request: Request, username: str):
    return [(token_hash("account:" + username), 5), (token_hash("ip:" + (request.client.host if request.client else "unknown")), 30)]

# 登录限流
def check_login_throttle(db: Session, request: Request, username: str) -> None:
    now = utcnow()
    for key, _limit in _throttle_keys(request, username):
        row = db.get(AuthThrottle, key)
        if row and row.locked_until and aware(row.locked_until) > now:
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后重试", headers={"Retry-After": str(max(1, int((aware(row.locked_until) - now).total_seconds())))})


def record_login_failure(db: Session, request: Request, username: str) -> None:
    now = utcnow()
    # Seed atomically so two first failures cannot race on the primary key.
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    for key, limit in _throttle_keys(request, username):
        db.execute(insert(AuthThrottle).values(key=key, failures=0, window_start=now)
                   .on_conflict_do_nothing(index_elements=["key"]))
        row = db.query(AuthThrottle).filter(AuthThrottle.key == key).populate_existing().with_for_update().one()
        if aware(row.window_start) < now - timedelta(minutes=15):
            row.failures = 0
            row.window_start = now
            row.locked_until = None
        row.failures += 1
        if row.failures >= limit:
            row.locked_until = now + timedelta(minutes=15)
    db.commit()

# 登录密码校验
def authenticate_password(db: Session, request: Request, username: str, password: str) -> User:
    check_login_throttle(db, request, username)
    user = db.query(User).filter(User.username == username).one_or_none()
    valid = verify_password(password, user.password_hash if user and user.password_hash else _DUMMY_PASSWORD_HASH)
    if user is None or not user.is_active or not user.password_hash or not valid:
        record_login_failure(db, request, username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    throttle = db.get(AuthThrottle, token_hash("account:" + username))
    if throttle:
        db.delete(throttle)
    return user
