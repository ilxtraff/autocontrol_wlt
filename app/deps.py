"""Общие зависимости FastAPI: текущий пользователь и проверка прав."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import User
from app.security import SESSION_COOKIE, read_session


class NotAuthenticated(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="нужен вход")


def current_user_optional(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    data = read_session(request.cookies.get(SESSION_COOKIE))
    if not data:
        return None
    user = session.get(User, data.get("uid"))
    if user is None or not user.is_active:
        return None
    return user


def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_global_editor(user: User = Depends(current_user)) -> User:
    if not user.can_edit_global_thresholds:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="общие пороги меняют только admin и group CEO",
        )
    return user


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    suffix = f"?next={target}" if target and target != "/" else ""
    return RedirectResponse(url=f"/login{suffix}", status_code=status.HTTP_303_SEE_OTHER)
