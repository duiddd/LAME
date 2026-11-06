from typing import Optional

from fastapi import APIRouter, Query

from ..errors import ErrorOut, not_implemented
from .schemas import CandlesOut, Interval, SummaryOut

router = APIRouter(prefix="/api/market", tags=["market"], responses={"4XX": {"model": ErrorOut}})


@router.get("/candles", response_model=CandlesOut)
async def candles(
    symbol: str,
    interval: Interval = "15s",
    limit: int = Query(default=300, ge=1, le=1000),
    after_day: Optional[str] = None,
    after_seq: Optional[int] = None,
):
    raise not_implemented()


@router.get("/summary", response_model=SummaryOut)
async def summary():
    raise not_implemented()
