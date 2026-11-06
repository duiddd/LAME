from typing import Optional

from fastapi import APIRouter

from ..errors import not_implemented

router = APIRouter(prefix="/api", tags=["push"])


@router.get("/stream")
async def stream(last_event_id: Optional[int] = None):
    """Server-Sent Events with the user's own `order_update` and `balance_update`."""
    raise not_implemented()
