from fastapi import APIRouter, Depends, Request, Response

from ..container import Container, get_container
from ..errors import AppError, ErrorOut
from .deps import ACCESS_COOKIE, REFRESH_COOKIE, current_user
from .schemas import Credentials, UserOut
from .service import Session
from .tokens import Principal

router = APIRouter(prefix="/api/auth", tags=["auth"], responses={"4XX": {"model": ErrorOut}})


def set_cookies(response: Response, session: Session, c: Container) -> None:
    secure = c.settings.cookie_secure
    response.set_cookie(ACCESS_COOKIE, session.access_token, max_age=c.settings.access_ttl_s, httponly=True,
                        samesite="lax", secure=secure, path="/")
    if session.refresh_token:
        response.set_cookie(REFRESH_COOKIE, session.refresh_token, max_age=c.settings.refresh_ttl_s, httponly=True,
                            samesite="lax", secure=secure, path="/api/auth")


@router.post("/register", response_model=UserOut, status_code=201)
async def register(body: Credentials, response: Response, c: Container = Depends(get_container)):
    session = await c.auth.register(body.username, body.password)
    set_cookies(response, session, c)
    return UserOut(user_id=session.user_id, username=session.username, role=session.role)


@router.post("/login", response_model=UserOut)
async def login(body: Credentials, response: Response, c: Container = Depends(get_container)):
    try:
        session = await c.auth.login(body.username, body.password)
    except AppError as e:
        if e.status == 401:
            c.metrics.auth_failures.inc()
        raise
    set_cookies(response, session, c)
    return UserOut(user_id=session.user_id, username=session.username, role=session.role)


@router.post("/refresh", status_code=204)
async def refresh(request: Request, response: Response, c: Container = Depends(get_container)):
    session = await c.auth.refresh(request.cookies.get(REFRESH_COOKIE))
    set_cookies(response, session, c)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, c: Container = Depends(get_container)):
    await c.auth.logout(request.cookies.get(REFRESH_COOKIE))
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")


@router.get("/me", response_model=UserOut)
async def me(user: Principal = Depends(current_user)):
    return UserOut(user_id=user.user_id, username=user.username, role=user.role)
