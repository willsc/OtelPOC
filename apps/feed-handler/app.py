"""Feed handler — simulated crypto market-data feed (publisher).

A background task consumes synthetic exchange feeds from multiple venues
(random-walk ticks per pair/venue), consolidates a best bid/offer book and
*pushes* it downstream to the trading engine — the realistic direction of
flow: the feed handler publishes, consumers subscribe. HTTP POST per batch
stands in for a WebSocket/multicast bus so trace context propagates
naturally.

Tracing / metrics / logging export is wired up by opentelemetry-instrument
(see Dockerfile CMD) — this file only contains *manual* instrumentation:
per-batch feed spans, tick counters and processing-latency histograms.
"""

import asyncio
import contextlib
import logging
import os
import random
import time

import httpx
from fastapi import FastAPI
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
    description="Feed tick normalisation time",
)
batches_published = meter.create_counter(
    "feed.batches.published",
    unit="{batch}",
    description="Consolidated book batches pushed downstream, by result",
)

TRADING_ENGINE_URL = os.environ.get("TRADING_ENGINE_URL", "http://trading-engine:8080")

VENUES = ["binance", "coinbase", "kraken"]

# pair -> reference price for the random walk
UNIVERSE = {
    "BTC/USDT": 97_000.0,
    "ETH/USDT": 3_500.0,
    "SOL/USDT": 210.0,
    "XRP/USDT": 2.40,
    "DOGE/USDT": 0.32,
}

# (pair, venue) -> last quote; consolidated BBO is derived across venues.
VENUE_BOOK: dict[tuple[str, str], dict] = {}


def _apply_tick(pair: str, venue: str) -> None:
    start = time.perf_counter()
    prev = VENUE_BOOK.get((pair, venue), {"mid": UNIVERSE[pair]})["mid"]
    mid = max(1e-6, prev * (1 + random.gauss(0, 0.0008)))
    spread = mid * random.uniform(0.0001, 0.0008)
    VENUE_BOOK[(pair, venue)] = {
        "mid": mid,
        "bid": mid - spread / 2,
        "ask": mid + spread / 2,
        "ts": time.time(),
    }
    # Simulated normalisation/book-build work.
    time.sleep(random.uniform(0.0001, 0.001))
    ticks_processed.add(1, {"pair": pair, "venue": venue})
    tick_latency.record(time.perf_counter() - start, {"venue": venue})


def _consolidated_bbo(pair: str) -> dict | None:
    quotes = [q for (p, v), q in VENUE_BOOK.items() if p == pair]
    if not quotes:
        return None
    return {
        "pair": pair,
        "bid": round(max(q["bid"] for q in quotes), 6),
        "ask": round(min(q["ask"] for q in quotes), 6),
        "ts": max(q["ts"] for q in quotes),
    }


async def feed_loop() -> None:
    """Consume venue feeds and publish the consolidated book downstream."""
    logger.info("feed handler subscribed: %d pairs x %d venues, publishing to %s",
                len(UNIVERSE), len(VENUES), TRADING_ENGINE_URL)
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            with tracer.start_as_current_span("publish-feed-batch") as span:
                batch = random.randint(50, 120)
                for _ in range(batch):
                    _apply_tick(random.choice(list(UNIVERSE)), random.choice(VENUES))
                span.set_attribute("feed.batch.size", batch)
                if random.random() < 0.02:
                    # Occasional venue hiccup: gap detected, resync that book.
                    venue = random.choice(VENUES)
                    span.add_event("feed gap detected", {"venue": venue})
                    logger.warning("feed gap detected on %s, resynced book", venue)

                quotes = [b for p in UNIVERSE if (b := _consolidated_bbo(p))]
                span.set_attribute("feed.quotes.published", len(quotes))
                try:
                    resp = await client.post(
                        f"{TRADING_ENGINE_URL}/marketdata", json={"quotes": quotes}
                    )
                    resp.raise_for_status()
                    batches_published.add(1, {"result": "ok"})
                except httpx.HTTPError as exc:
                    batches_published.add(1, {"result": "error"})
                    span.set_attribute("feed.publish.error", str(exc))
                    logger.warning("failed to publish batch downstream: %s", exc)
            await asyncio.sleep(1.0)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(feed_loop())
    yield
    task.cancel()


app = FastAPI(title="feed-handler", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "books": len(VENUE_BOOK)}
