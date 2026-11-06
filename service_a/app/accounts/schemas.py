from pydantic import BaseModel


class BalanceOut(BaseModel):
    asset: str
    available: str
    held: str
    total: str


class AccountOut(BaseModel):
    user_id: int
    username: str
    balances: list[BalanceOut]


class DepositIn(BaseModel):
    asset: str
    amount: str


class DepositOut(BaseModel):
    deposit_id: int
    asset: str
    amount: str
    balance: BalanceOut
