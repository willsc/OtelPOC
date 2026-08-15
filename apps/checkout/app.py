"""Checkout service — public entry point of the demo shop.

Receives orders, records custom metrics, and calls the payments service.
Tracing / metrics / logging export is wired up by opentelemetry-instrument
(see Dockerfile CMD) — this file only contains *manual* instrumentation:
custom spans, span attributes, counters and histograms.
"""

import logging
import os
import random
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("checkout")

tracer = trace.get_tracer("checkout")
meter = metrics.get_meter("checkout")

orders_created = meter.create_counter(
    "orders.created", unit="{order}", description="Successfully placed orders"
)
orders_failed = meter.create_counter(
    "orders.failed", unit="{order}", description="Orders that could not be placed"
)
order_value = meter.create_histogram(
    "order.value", unit="USD", description="Value of placed orders"
)

PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://payments:8081")

CATALOG = {
    "keyboard": 89.0,
    "mouse": 45.5,
    "monitor": 329.0,
    "webcam": 59.99,
    "headset": 120.0,
}

app = FastAPI(title="checkout")


class OrderRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    items: dict[str, int] = Field(min_length=1, description="item name -> quantity")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/checkout")
async def checkout(order: OrderRequest):
    order_id = str(uuid.uuid4())[:8]
    span = trace.get_current_span()
    span.set_attribute("order.id", order_id)
    span.set_attribute("customer.id", order.customer_id)

    unknown = [name for name in order.items if name not in CATALOG]
    if unknown:
        orders_failed.add(1, {"reason": "unknown_item"})
        logger.warning("order %s rejected, unknown items: %s", order_id, unknown)
        raise HTTPException(status_code=400, detail=f"unknown items: {unknown}")

    with tracer.start_as_current_span("price-order") as price_span:
        total = sum(CATALOG[name] * qty for name, qty in order.items.items())
        price_span.set_attribute("order.total", total)

    logger.info(
        "order %s for customer %s: %d item type(s), total %.2f USD",
        order_id, order.customer_id, len(order.items), total,
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{PAYMENTS_URL}/charge",
            json={"order_id": order_id, "amount": total, "customer_id": order.customer_id},
        )

    if resp.status_code == 402:
        orders_failed.add(1, {"reason": "payment_declined"})
        logger.warning("order %s: payment declined for %.2f USD", order_id, total)
        raise HTTPException(status_code=402, detail="payment declined")
    if resp.status_code != 200:
        orders_failed.add(1, {"reason": "payment_error"})
        logger.error("order %s: payment service error %d", order_id, resp.status_code)
        raise HTTPException(status_code=502, detail="payment service error")

    orders_created.add(1)
    order_value.record(total)
    logger.info("order %s placed successfully (%.2f USD)", order_id, total)
    return {"order_id": order_id, "total": total, "status": "confirmed"}


@app.get("/catalog")
async def catalog():
    # Simulate an occasionally slow catalog lookup so latency panels move.
    with tracer.start_as_current_span("load-catalog") as span:
        if random.random() < 0.05:
            import asyncio

            await asyncio.sleep(random.uniform(0.5, 1.5))
            span.set_attribute("catalog.cache_hit", False)
        else:
            span.set_attribute("catalog.cache_hit", True)
    return CATALOG
