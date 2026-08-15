"""Continuous order-flow generator for the crypto trading engine.

Mostly valid orders across the tradable pairs, plus the occasional delisted
pair and notional-breaching order so every signal (fill, 4xx reject,
5xx error) shows up in the data.
"""

import logging
import os
import random
import time

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loadgen")

TRADING_ENGINE_URL = os.environ.get("TRADING_ENGINE_URL", "http://trading-engine:8080")

# pair -> typical order size range in base asset
PAIRS = {
    "BTC/USDT": (0.001, 0.5),
    "ETH/USDT": (0.01, 10.0),
    "SOL/USDT": (0.5, 200.0),
    "XRP/USDT": (50.0, 20_000.0),
    "DOGE/USDT": (100.0, 100_000.0),
}
ACCOUNTS = [f"ACC-{i:04d}" for i in range(1, 21)]


def random_order() -> dict:
    pair = random.choice(list(PAIRS))
    lo, hi = PAIRS[pair]
    return {
        "account_id": random.choice(ACCOUNTS),
        "pair": pair,
        "side": random.choice(["buy", "sell"]),
        "amount": round(random.uniform(lo, hi), 6),
    }


def main() -> None:
    client = httpx.Client(timeout=15.0)
    log.info("order-flow generator started against %s", TRADING_ENGINE_URL)
    while True:
        try:
            order = random_order()
            roll = random.random()
            if roll < 0.05:
                order["pair"] = "LUNA/USDT"  # delisted -> 400
            elif roll < 0.10:
                order["amount"] = PAIRS.get(order["pair"], (1, 1))[1] * 100  # breaches notional -> 409
            r = client.post(f"{TRADING_ENGINE_URL}/orders", json=order)
            log.info("%s %s %s -> %d", order["side"], order["amount"], order["pair"], r.status_code)
        except httpx.HTTPError as exc:
            log.warning("request failed: %s", exc)
        time.sleep(random.uniform(0.2, 1.0))


if __name__ == "__main__":
    main()
