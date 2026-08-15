"""Trading engine — order entry point of the demo crypto trading system.

Subscribes to the consolidated market data pushed by the feed handler
(POST /marketdata), maintains a local top-of-book cache, and executes
incoming orders against it — no network round-trip on the order path,
as in a real trading system.

Tracing / metrics / logging export is wired up by opentelemetry-instrument
(see Dockerfile CMD) — this file only contains *manual* instrumentation:
custom spans, span attributes, counters and histograms.
"""

import asyncio
import logging
import random
import time
import uuid
from typing import Literal

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
ticks_applied = meter.create_counter(
    "marketdata.quotes.applied", unit="{quote}",
    description="Market data updates applied to the local book",
)
quote_staleness = meter.create_histogram(
    "order.quote.staleness", unit="s",
    description="Age of the cached quote at execution time",
)

TRADABLE = {"BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"}
MAX_ORDER_NOTIONAL_USD = 250_000.0
VENUES = ["binance", "coinbase", "kraken"]

# Local consolidated top-of-book, kept fresh by the feed handler's pushes.
BOOK: dict[str, dict] = {}

app = FastAPI(title="trading-engine")


class Quote(BaseModel):
    pair: str
    bid: float
    ask: float
    ts: float


class MarketDataBatch(BaseModel):
    quotes: list[Quote] = Field(min_length=1)


class OrderRequest(BaseModel):
    account_id: str = Field(min_length=1)
    pair: str
    side: Literal["buy", "sell"]
    amount: float = Field(gt=0, description="quantity in base asset")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "book_pairs": len(BOOK)}


@app.post("/marketdata")
async def marketdata(batch: MarketDataBatch):
    span = trace.get_current_span()
    for q in batch.quotes:
        BOOK[q.pair] = {"bid": q.bid, "ask": q.ask, "ts": q.ts}
        ticks_applied.add(1, {"pair": q.pair})
    span.set_attribute("marketdata.quotes.applied", len(batch.quotes))
    return {"applied": len(batch.quotes)}


@app.post("/orders")
async def submit_order(order: OrderRequest):
    order_id = str(uuid.uuid4())[:8]
    span = trace.get_current_span()
    span.set_attribute("order.id", order_id)
    span.set_attribute("order.account_id", order.account_id)
    span.set_attribute("order.pair", order.pair)
    span.set_attribute("order.side", order.side)
    span.set_attribute("order.amount", order.amount)

    if order.pair not in TRADABLE:
        orders_processed.add(1, {"outcome": "rejected_pair"})
        logger.warning("order %s rejected: %s not tradable", order_id, order.pair)
        raise HTTPException(status_code=400, detail=f"pair {order.pair} not tradable")

    with tracer.start_as_current_span("book-lookup") as book_span:
        q = BOOK.get(order.pair)
        if q is None:
            orders_processed.add(1, {"outcome": "rejected_no_marketdata"})
            logger.error("order %s: no market data yet for %s", order_id, order.pair)
            raise HTTPException(status_code=503, detail="no market data for pair")
        staleness = max(0.0, time.time() - q["ts"])
        quote_staleness.record(staleness, {"pair": order.pair})
        book_span.set_attribute("book.quote.staleness_s", round(staleness, 3))
        if staleness > 10:
            book_span.add_event("stale quote at execution time")
            logger.warning("order %s: quote for %s is %.1fs stale",
                           order_id, order.pair, staleness)
        mid = (q["bid"] + q["ask"]) / 2

    with tracer.start_as_current_span("pre-trade-risk-check") as risk_span:
        notional_est = order.amount * mid
        risk_span.set_attribute("risk.max_notional_usd", MAX_ORDER_NOTIONAL_USD)
        risk_span.set_attribute("risk.order_notional_usd", round(notional_est, 2))
        if notional_est > MAX_ORDER_NOTIONAL_USD:
            orders_processed.add(1, {"outcome": "rejected_risk"})
            logger.warning(
                "order %s rejected by risk: notional %.0f USD > limit %.0f (%s %s %s)",
                order_id, notional_est, MAX_ORDER_NOTIONAL_USD,
                order.side, order.amount, order.pair,
            )
            raise HTTPException(status_code=409, detail="rejected by pre-trade risk: notional limit")

    with tracer.start_as_current_span("execute-order") as exec_span:
        start = asyncio.get_event_loop().time()
        venue = random.choice(VENUES)
        exec_span.set_attribute("execution.venue", venue)
        # Venue round-trip, with an occasional slow outlier.
        delay = random.uniform(0.005, 0.05)
        if random.random() < 0.03:
            delay = random.uniform(1.0, 2.0)
            exec_span.set_attribute("execution.slow_path", True)
        await asyncio.sleep(delay)

        roll = random.random()
        if roll < 0.03:
            orders_processed.add(1, {"outcome": "error"})
            logger.error("order %s failed: %s session lost", order_id, venue)
            raise RuntimeError(f"{venue} session lost")  # -> 500 + error span
        if roll < 0.08:
            orders_processed.add(1, {"outcome": "rejected_liquidity"})
            logger.warning("order %s rejected: insufficient liquidity for %s %s on %s",
                           order_id, order.amount, order.pair, venue)
            raise HTTPException(status_code=409, detail="insufficient liquidity")

        ref = q["ask"] if order.side == "buy" else q["bid"]
        slippage = ref * random.uniform(0, 0.0005)
        fill_price = ref + slippage if order.side == "buy" else ref - slippage
        notional = round(fill_price * order.amount, 2)
        execution_latency.record(asyncio.get_event_loop().time() - start)
        exec_span.set_attribute("execution.fill_price", round(fill_price, 6))
        exec_span.set_attribute("execution.notional_usd", notional)

    orders_processed.add(1, {"outcome": "filled", "side": order.side})
    trade_notional.record(notional, {"pair": order.pair, "side": order.side})
    logger.info("order %s filled on %s: %s %s %s @ %.6f (notional %.2f USD)",
                order_id, venue, order.side, order.amount, order.pair,
                fill_price, notional)
    return {
        "order_id": order_id,
        "status": "filled",
        "pair": order.pair,
        "side": order.side,
        "amount": order.amount,
        "venue": venue,
        "fill_price": round(fill_price, 6),
        "notional_usd": notional,
    }
