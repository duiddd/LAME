from typing import Literal, Optional

from pydantic import BaseModel

Interval = Literal["5s", "15s", "1m", "5m"]


class CandleOut(BaseModel):
    t: int
    o: str
    h: str
    l: str
    c: str
    v: str


class Watermark(BaseModel):
    b_day: str
    b_seq: int


class CandlesOut(BaseModel):
    symbol: str
    interval: Interval
    candles: list[CandleOut]
    watermark: Optional[Watermark]
    replace_all: bool


class SymbolSummaryOut(BaseModel):
    symbol: str
    base: str
    quote: str
    tick: str
    lot: str
    price_decimals: int
    qty_decimals: int
    day: Optional[str]
    day_open: Optional[str]


class SummaryOut(BaseModel):
    symbols: list[SymbolSummaryOut]
