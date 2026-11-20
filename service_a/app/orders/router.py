from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, Query

from ..auth.deps import current_user
from ..auth.tokens import Principal
from ..container import Container, get_container
from ..errors import ErrorOut
from .schemas import FillListOut, ModifyIn, OrderDetailOut, OrderIn, OrderListOut, OrderOut

router = APIRouter(prefix="/api", tags=["orders"], responses={"4XX": {"model": ErrorOut}, "503": {"model": ErrorOut}})


@router.post("/orders", response_model=OrderOut, status_code=202)
async def place_order(body: OrderIn, idempotency_key: str = Header(min_length=1, max_length=64),
                      user: Principal = Depends(current_user), c: Container = Depends(get_container)):
    return c.views.order_out(await c.orders.place(user.user_id, body, idempotency_key))


@router.get("/orders", response_model=OrderListOut)
async def list_orders(status: Optional[Literal["open", "history"]] = None, limit: int = Query(default=100, ge=1, le=500),
                      user: Principal = Depends(current_user), c: Container = Depends(get_container)):
    rows = await c.orders.list_for_user(user.user_id, status, limit)
    return OrderListOut(orders=[c.views.order_out(r) for r in rows])


@router.get("/orders/{order_id}", response_model=OrderDetailOut)
async def get_order(order_id: str, user: Principal = Depends(current_user), c: Container = Depends(get_container)):
    row, fills = await c.orders.get_owned(user.user_id, order_id)
    return OrderDetailOut(**c.views.order_out(row).model_dump(), fills=[c.views.fill_out(f) for f in fills])


@router.put("/orders/{order_id}", response_model=OrderOut, status_code=202)
async def modify_order(order_id: str, body: ModifyIn, user: Principal = Depends(current_user), c: Container = Depends(get_container)):
    return c.views.order_out(await c.orders.modify(user.user_id, order_id, body))


@router.delete("/orders/{order_id}", response_model=OrderOut, status_code=202)
async def cancel_order(order_id: str, user: Principal = Depends(current_user), c: Container = Depends(get_container)):
    return c.views.order_out(await c.orders.cancel(user.user_id, order_id))


@router.get("/fills", response_model=FillListOut)
async def list_fills(limit: int = Query(default=100, ge=1, le=500), user: Principal = Depends(current_user), c: Container = Depends(get_container)):
    return FillListOut(fills=[c.views.fill_out(f) for f in await c.orders.fills_for_user(user.user_id, limit)])
