"""Feed handler — simulated market-data feed.

A background task consumes a synthetic exchange feed (random-walk ticks per
symbol), keeps the latest top-of-book quote in memory and serves it to the
trading engine over HTTP.

Tracing / metrics / logging export is wired up by opentelemetry-instrument
(see Dockerfile CMD) — this file only contains *manual* instrumentation:
per-batch feed spans, tick counters and processing-latency histograms.
"""

import asyncio
import contextlib
import logging
import random
import time

from fastapi import FastAPI, HTTPException
from opentelemetry import metrics, trace

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("feed-handler")

tracer = trace.get_tracer("feed-handler")
meter = metrics.get_meter("feed-handler")

ticks_processed = meter.create_counter(
    "feed.ticks.processed", unit="{tick}", description="Market data ticks processed"
)
tick_latency = meter.create_histogram(
    "feed.tick.processing.duration",
    unit="s",
    description="Simulated feed tick processing time",
)
quotes_served = meter.create_counter(
    "feed.quotes.served", unit="{quote}", description="Quote lookups served"
)

# symbol -> reference price for the random walk
UNIVERSE = {
    "AAPL": 230.0,
    "MSFT": 425.0,
    "NVDA": 135.0,
    "AMZN": 185.0,
    "TSLA": 250.0,
}

BOOK: dict[str, dict] = {}


def _apply_tick(symbol: str) -> None:
    start = time.perf_counter()
    prev = BOOK.get(symbol, {"mid": UNIVERSE[symbol]})["mid"]
    mid = max(0.01, prev * (1 + random.gauss(0, 0.0005)))
    spread = mid * random.uniform(0.0001, 0.0005)
    BOOK[symbol] = {
        "mid": mid,
        "bid": round(mid - spread / 2, 4),
        "ask": round(mid + spread / 2, 4),
        "ts": time.time(),
    }
    # Simulated normalisation/book-build work.
    time.sleep(random.uniform(0.0001, 0.001))
    ticks_processed.add(1, {"symbol": symbol})
    tick_latency.record(time.perf_counter() - start, {"symbol": symbol})


async def feed_loop() -> None:
    """Consume the synthetic exchange feed, one traced span per batch."""
    logger.info("feed handler subscribed to %d symbols", len(UNIVERSE))
    while True:
        with tracer.start_as_current_span("process-feed-batch") as span:
            batch = random.randint(30, 60)
            for _ in range(batch):
                _apply_tick(random.choice(list(UNIVERSE)))
            span.set_attribute("feed.batch.size", batch)
            if random.random() < 0.02:
                # Occasional feed hiccup: gap detected, resync the book.
                span.add_event("feed gap detected, resyncing book")
                logger.warning("feed gap detected, resynced book (batch=%d)", batch)
        await asyncio.sleep(1.0)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(feed_loop())
    yield
    task.cancel()


app = FastAPI(title="feed-handler", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "symbols": len(BOOK)}


@app.get("/quote/{symbol}")
async def quote(symbol: str):
    span = trace.get_current_span()
    span.set_attribute("market.symbol", symbol)
    q = BOOK.get(symbol)
    if q is None:
        quotes_served.add(1, {"symbol": symbol, "result": "unknown"})
        logger.warning("quote requested for unknown symbol %s", symbol)
        raise HTTPException(status_code=404, detail=f"unknown symbol {symbol}")

    if random.random() < 0.05:
        # Simulated slow path: quote not in hot cache, rebuild from depth.
        with tracer.start_as_current_span("rebuild-from-depth"):
            await asyncio.sleep(random.uniform(0.1, 0.6))

    quotes_served.add(1, {"symbol": symbol, "result": "ok"})
    return {"symbol": symbol, **{k: q[k] for k in ("bid", "ask", "ts")}}
