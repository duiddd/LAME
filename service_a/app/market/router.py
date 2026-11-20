from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..container import Container, get_container
from ..errors import ErrorOut
from .schemas import CandlesOut, Interval, SummaryOut

router = APIRouter(prefix="/api/market", tags=["market"], responses={"4XX": {"model": ErrorOut}})


@router.get("/candles", response_model=CandlesOut)
async def candles(symbol: str, interval: Interval = "15s", limit: int = Query(default=300, ge=1, le=1000),
                  after_day: Optional[str] = None, after_seq: Optional[int] = None, c: Container = Depends(get_container)):
    return await c.market.candles(symbol, interval, limit, after_day, after_seq)


@router.get("/summary", response_model=SummaryOut)
async def summary(c: Container = Depends(get_container)):
    return await c.market.summary()
