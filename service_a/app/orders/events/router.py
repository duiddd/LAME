from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ...container import Container, get_container

router = APIRouter(tags=["internal"])


class NotificationHead(BaseModel):
    notify_seq: int
    type: str


@router.post("/internal/order-events", include_in_schema=False)
async def order_events(request: Request, c: Container = Depends(get_container)):
    """D's notifications. Bodies are plain top-level JSON: D reads `expected_seq` directly."""
    try:
        body = await request.json()
        NotificationHead.model_validate(body)
    except (ValueError, ValidationError) as e:
        c.metrics.notify_failures.labels("MALFORMED").inc()
        return JSONResponse({"reason": "MALFORMED", "message": str(e)}, status_code=422)
    outcome = await c.events.handle(body)
    return JSONResponse(outcome.body, status_code=outcome.status)
