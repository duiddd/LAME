from typing import Literal, Optional

from fastapi import APIRouter, Header

from ..errors import ErrorOut, not_implemented
from .schemas import FillListOut, ModifyIn, OrderDetailOut, OrderIn, OrderListOut, OrderOut

router = APIRouter(prefix="/api", tags=["orders"], responses={"4XX": {"model": ErrorOut}, "503": {"model": ErrorOut}})


@router.post("/orders", response_model=OrderOut, status_code=202)
async def place_order(body: OrderIn, idempotency_key: str = Header(min_length=1, max_length=64)):
    raise not_implemented()


@router.get("/orders", response_model=OrderListOut)
async def list_orders(status: Optional[Literal["open", "history"]] = None, limit: int = 100):
    raise not_implemented()


@router.get("/orders/{order_id}", response_model=OrderDetailOut)
async def get_order(order_id: str):
    raise not_implemented()


@router.put("/orders/{order_id}", response_model=OrderOut, status_code=202)
async def modify_order(order_id: str, body: ModifyIn):
    raise not_implemented()


@router.delete("/orders/{order_id}", response_model=OrderOut, status_code=202)
async def cancel_order(order_id: str):
    raise not_implemented()


@router.get("/fills", response_model=FillListOut)
async def list_fills(limit: int = 100):
    raise not_implemented()
