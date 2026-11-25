import time

import bcrypt
import jwt
from cryptography.fernet import Fernet
from fastapi import Cookie, Header, HTTPException

import config
import db

_fernet = Fernet(config.TOKEN_ENC_KEY.encode())
SESSION_SECONDS = 7 * 24 * 3600


BCRYPT_ROUNDS = 10  # still a strong setting, and about four times quicker than the default 12 on the small free server


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def needs_rehash(password_hash: str) -> bool:
    try:
        return int(password_hash.split("$")[2]) > BCRYPT_ROUNDS
    except Exception:
        return False


def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def make_session(user_id: int) -> str:
    return jwt.encode(
        {"sub": str(user_id), "exp": int(time.time()) + SESSION_SECONDS}, config.JWT_SECRET, algorithm="HS256"
    )


def current_user(authorization: str | None = Header(default=None), session: str | None = Cookie(default=None)):
    """The Opportune frontend sends `Authorization: Bearer <jwt>`; a session cookie also works."""
    token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else session
    if not token:
        raise HTTPException(401, "Not signed in")
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired. Sign in again")
    user = db.get_user(int(payload["sub"]))
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode()).decode()


def decrypt(text: str) -> str:
    return _fernet.decrypt(text.encode()).decode()