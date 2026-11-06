from fastapi import APIRouter

from ..errors import ErrorOut, not_implemented
from .schemas import Credentials, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"], responses={"4XX": {"model": ErrorOut}})


@router.post("/register", response_model=UserOut, status_code=201)
async def register(body: Credentials):
    raise not_implemented()


@router.post("/login", response_model=UserOut)
async def login(body: Credentials):
    raise not_implemented()


@router.post("/refresh", status_code=204)
async def refresh():
    raise not_implemented()


@router.post("/logout", status_code=204)
async def logout():
    raise not_implemented()


@router.get("/me", response_model=UserOut)
async def me():
    raise not_implemented()
