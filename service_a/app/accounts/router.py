from fastapi import APIRouter, Header

from ..errors import ErrorOut, not_implemented
from .schemas import AccountOut, DepositIn, DepositOut

router = APIRouter(prefix="/api/accounts", tags=["accounts"], responses={"4XX": {"model": ErrorOut}})


@router.get("/me", response_model=AccountOut)
async def my_account():
    raise not_implemented()


@router.post("/deposits", response_model=DepositOut, status_code=201)
async def deposit(body: DepositIn, idempotency_key: str = Header(min_length=1, max_length=64)):
    raise not_implemented()
