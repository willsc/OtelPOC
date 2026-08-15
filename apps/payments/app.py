"""Payments service — downstream dependency of checkout.

Simulates a payment provider with realistic behaviour: variable latency,
~8% declines, ~3% internal errors and the occasional very slow response.
"""

import asyncio
import logging
import random

from fastapi import FastAPI, HTTPException
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payments")

tracer = trace.get_tracer("payments")
meter = metrics.get_meter("payments")

payments_processed = meter.create_counter(
    "payments.processed", unit="{payment}", description="Processed payment attempts"
)
payment_amount = meter.create_histogram(
    "payment.amount", unit="USD", description="Amount per payment attempt"
)

app = FastAPI(title="payments")


class ChargeRequest(BaseModel):
    order_id: str
    customer_id: str
    amount: float = Field(gt=0)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/charge")
async def charge(req: ChargeRequest):
    span = trace.get_current_span()
    span.set_attribute("order.id", req.order_id)
    span.set_attribute("payment.amount", req.amount)
    payment_amount.record(req.amount)

    with tracer.start_as_current_span("acquirer-call") as acquirer_span:
        # Normal processing time, with an occasional slow outlier.
        delay = random.uniform(0.02, 0.25)
        if random.random() < 0.03:
            delay = random.uniform(1.0, 2.5)
            acquirer_span.set_attribute("acquirer.slow_path", True)
        await asyncio.sleep(delay)

    roll = random.random()
    if roll < 0.03:
        payments_processed.add(1, {"outcome": "error"})
        logger.error("charge for order %s failed: acquirer unavailable", req.order_id)
        raise RuntimeError("acquirer connection reset")  # -> 500 + error span
    if roll < 0.11:
        payments_processed.add(1, {"outcome": "declined"})
        logger.warning(
            "charge for order %s declined (%.2f USD)", req.order_id, req.amount
        )
        raise HTTPException(status_code=402, detail="card declined")

    payments_processed.add(1, {"outcome": "approved"})
    logger.info("charge for order %s approved (%.2f USD)", req.order_id, req.amount)
    return {"order_id": req.order_id, "status": "approved"}
