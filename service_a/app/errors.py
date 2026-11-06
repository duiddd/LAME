"""Business errors, and their one mapping to an HTTP status and a machine-readable reason."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorOut(BaseModel):
    reason: str
    message: str


class AppError(Exception):
    """Raised by services; they do not import FastAPI's HTTPException."""

    def __init__(self, status: int, reason: str, message: str = ""):
        super().__init__(f"{reason}: {message}")
        self.status = status
        self.reason = reason
        self.message = message or reason


def not_implemented() -> AppError:
    return AppError(501, "NOT_IMPLEMENTED", "This endpoint is part of a later milestone")


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def on_app_error(_: Request, exc: AppError):
        return JSONResponse({"reason": exc.reason, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(_: Request, exc: RequestValidationError):
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        return JSONResponse({"reason": "INVALID_REQUEST", "message": f"Invalid fields: {fields}"}, status_code=422)
