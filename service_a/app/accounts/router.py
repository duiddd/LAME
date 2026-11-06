from fastapi import APIRouter, Depends, Header

from ..auth.deps import current_user
from ..auth.tokens import Principal
from ..config import from_units
from ..container import Container, get_container
from ..errors import ErrorOut
from .schemas import AccountOut, DepositIn, DepositOut

router = APIRouter(prefix="/api/accounts", tags=["accounts"], responses={"4XX": {"model": ErrorOut}})


@router.get("/me", response_model=AccountOut)
async def my_account(user: Principal = Depends(current_user), c: Container = Depends(get_container)):
    return AccountOut(user_id=user.user_id, username=user.username, balances=await c.accounts.my_balances(user.user_id))


@router.post("/deposits", response_model=DepositOut, status_code=201)
async def deposit(body: DepositIn, idempotency_key: str = Header(min_length=1, max_length=64),
                  user: Principal = Depends(current_user), c: Container = Depends(get_container)):
    deposit_id, asset, amount, balance = await c.accounts.deposit(user.user_id, body.asset, body.amount, idempotency_key)
    return DepositOut(deposit_id=deposit_id, asset=asset, amount=from_units(amount, c.catalog.assets[asset].decimals), balance=balance)
