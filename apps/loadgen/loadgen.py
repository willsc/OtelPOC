"""Continuous order-flow generator for the trading engine.

Mostly valid orders across the tradable universe, plus the occasional
non-tradable symbol and oversized (risk-breaching) order so every signal
(fill, 4xx reject, 5xx error) shows up in the data.
"""

import logging
import os
import random
import time

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loadgen")

TRADING_ENGINE_URL = os.environ.get("TRADING_ENGINE_URL", "http://trading-engine:8080")

SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA"]
ACCOUNTS = [f"ACC-{i:04d}" for i in range(1, 21)]


def random_order() -> dict:
    return {
        "account_id": random.choice(ACCOUNTS),
        "symbol": random.choice(SYMBOLS),
        "side": random.choice(["buy", "sell"]),
        "qty": max(1, int(random.lognormvariate(4.5, 1.2))),
    }


def main() -> None:
    client = httpx.Client(timeout=15.0)
    log.info("order-flow generator started against %s", TRADING_ENGINE_URL)
    while True:
        try:
            order = random_order()
            roll = random.random()
            if roll < 0.05:
                order["symbol"] = "ENRN"  # not tradable -> 400
            elif roll < 0.10:
                order["qty"] = random.randint(20_000, 80_000)  # breaches risk -> 409
            r = client.post(f"{TRADING_ENGINE_URL}/orders", json=order)
            log.info("%s %d %s -> %d", order["side"], order["qty"], order["symbol"], r.status_code)
        except httpx.HTTPError as exc:
            log.warning("request failed: %s", exc)
        time.sleep(random.uniform(0.2, 1.0))


if __name__ == "__main__":
    main()
