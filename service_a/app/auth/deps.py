from fastapi import Depends, Request

from ..container import Container, get_container
from ..errors import AppError
from .tokens import Principal

ACCESS_COOKIE = "lame_access"
REFRESH_COOKIE = "lame_refresh"


def current_user(request: Request, c: Container = Depends(get_container)) -> Principal:
    token = request.cookies.get(ACCESS_COOKIE)
    principal = c.tokens.verify_access(token) if token else None
    if principal is None:
        raise AppError(401, "UNAUTHORIZED", "Log in first")
    return principal
