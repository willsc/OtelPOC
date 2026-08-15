"""Trading engine — order entry point of the demo trading system.

Validates incoming orders, runs a pre-trade risk check, fetches the current
quote from the feed handler and simulates execution against it.

Tracing / metrics / logging export is wired up by opentelemetry-instrument
(see Dockerfile CMD) — this file only contains *manual* instrumentation:
custom spans, span attributes, counters and histograms.
"""

import asyncio
import logging
import os
import random
import uuid
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading-engine")

tracer = trace.get_tracer("trading-engine")
meter = metrics.get_meter("trading-engine")

orders_processed = meter.create_counter(
    "orders.processed", unit="{order}", description="Orders by final outcome"
)
execution_latency = meter.create_histogram(
    "order.execution.duration", unit="s", description="Time spent in execution"
)
trade_notional = meter.create_histogram(
    "trade.notional", unit="USD", description="Notional value of filled trades"
)

FEED_HANDLER_URL = os.environ.get("FEED_HANDLER_URL", "http://feed-handler:8081")

TRADABLE = {"AAPL", "MSFT", "NVDA", "AMZN", "TSLA"}
MAX_ORDER_QTY = 10_000

app = FastAPI(title="trading-engine")


class OrderRequest(BaseModel):
    account_id: str = Field(min_length=1)
    symbol: str
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/orders")
async def submit_order(order: OrderRequest):
    order_id = str(uuid.uuid4())[:8]
    span = trace.get_current_span()
    span.set_attribute("order.id", order_id)
    span.set_attribute("order.account_id", order.account_id)
    span.set_attribute("order.symbol", order.symbol)
    span.set_attribute("order.side", order.side)
    span.set_attribute("order.qty", order.qty)

    if order.symbol not in TRADABLE:
        orders_processed.add(1, {"outcome": "rejected_symbol"})
        logger.warning("order %s rejected: %s not tradable", order_id, order.symbol)
        raise HTTPException(status_code=400, detail=f"symbol {order.symbol} not tradable")

    with tracer.start_as_current_span("pre-trade-risk-check") as risk_span:
        risk_span.set_attribute("risk.max_qty", MAX_ORDER_QTY)
        if order.qty > MAX_ORDER_QTY:
            orders_processed.add(1, {"outcome": "rejected_risk"})
            logger.warning(
                "order %s rejected by risk: qty %d > limit %d (%s %s)",
                order_id, order.qty, MAX_ORDER_QTY, order.side, order.symbol,
            )
            raise HTTPException(status_code=409, detail="rejected by pre-trade risk: qty limit")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{FEED_HANDLER_URL}/quote/{order.symbol}")
    if resp.status_code != 200:
        orders_processed.add(1, {"outcome": "rejected_no_quote"})
        logger.error("order %s: no quote for %s (feed returned %d)",
                     order_id, order.symbol, resp.status_code)
        raise HTTPException(status_code=502, detail="no market data for symbol")
    q = resp.json()

    with tracer.start_as_current_span("execute-order") as exec_span:
        start = asyncio.get_event_loop().time()
        # Matching/venue latency, with an occasional slow outlier.
        delay = random.uniform(0.005, 0.05)
        if random.random() < 0.03:
            delay = random.uniform(1.0, 2.0)
            exec_span.set_attribute("execution.slow_path", True)
        await asyncio.sleep(delay)

        roll = random.random()
        if roll < 0.03:
            orders_processed.add(1, {"outcome": "error"})
            logger.error("order %s failed: venue session lost", order_id)
            raise RuntimeError("venue session lost")  # -> 500 + error span
        if roll < 0.08:
            orders_processed.add(1, {"outcome": "rejected_liquidity"})
            logger.warning("order %s rejected: insufficient liquidity for %d %s",
                           order_id, order.qty, order.symbol)
            raise HTTPException(status_code=409, detail="insufficient liquidity")

        ref = q["ask"] if order.side == "buy" else q["bid"]
        slippage = ref * random.uniform(0, 0.0002)
        fill_price = round(ref + slippage if order.side == "buy" else ref - slippage, 4)
        notional = round(fill_price * order.qty, 2)
        execution_latency.record(asyncio.get_event_loop().time() - start)
        exec_span.set_attribute("execution.fill_price", fill_price)
        exec_span.set_attribute("execution.notional", notional)

    orders_processed.add(1, {"outcome": "filled", "side": order.side})
    trade_notional.record(notional, {"symbol": order.symbol, "side": order.side})
    logger.info("order %s filled: %s %d %s @ %.4f (notional %.2f USD)",
                order_id, order.side, order.qty, order.symbol, fill_price, notional)
    return {
        "order_id": order_id,
        "status": "filled",
        "symbol": order.symbol,
        "side": order.side,
        "qty": order.qty,
        "fill_price": fill_price,
        "notional": notional,
    }
