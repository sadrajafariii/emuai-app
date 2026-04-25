"""
JWT-based auth utilities for bud.
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Request, HTTPException

SECRET_KEY = os.getenv("JWT_SECRET", "bud-jwt-secret-please-change-in-production")
ALGORITHM  = "HS256"
EXPIRE_DAYS = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: str, email: str) -> str:
    expire = datetime.utcnow() + timedelta(days=EXPIRE_DAYS)
    return jwt.encode(
        {"sub": user_id, "email": email, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_current_user(request: Request) -> dict:
    """FastAPI dependency — raises 401 if not authenticated."""
    token = request.cookies.get("bud_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"id": payload["sub"], "email": payload["email"]}


def get_user_from_ws(websocket) -> Optional[dict]:
    """Extract user from WebSocket cookie (no exception — returns None if invalid)."""
    token = websocket.cookies.get("bud_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    return {"id": payload["sub"], "email": payload["email"]}
