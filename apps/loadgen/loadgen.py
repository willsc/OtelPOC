"""Continuous load generator for the checkout service.

Mostly valid orders, a few browses of the catalog, and the occasional
bad request so every signal (success, 4xx, 5xx) shows up in the data.
"""

import logging
import os
import random
import time

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loadgen")

CHECKOUT_URL = os.environ.get("CHECKOUT_URL", "http://checkout:8080")

ITEMS = ["keyboard", "mouse", "monitor", "webcam", "headset"]
CUSTOMERS = [f"cust-{i:03d}" for i in range(1, 26)]


def random_order() -> dict:
    items = {name: random.randint(1, 3) for name in random.sample(ITEMS, k=random.randint(1, 3))}
    return {"customer_id": random.choice(CUSTOMERS), "items": items}


def main() -> None:
    client = httpx.Client(timeout=15.0)
    log.info("load generator started against %s", CHECKOUT_URL)
    while True:
        try:
            roll = random.random()
            if roll < 0.15:
                r = client.get(f"{CHECKOUT_URL}/catalog")
                log.info("GET /catalog -> %d", r.status_code)
            elif roll < 0.20:
                # Invalid order: unknown item -> 400 from checkout.
                bad = {"customer_id": random.choice(CUSTOMERS), "items": {"flux-capacitor": 1}}
                r = client.post(f"{CHECKOUT_URL}/checkout", json=bad)
                log.info("POST /checkout (bad item) -> %d", r.status_code)
            else:
                r = client.post(f"{CHECKOUT_URL}/checkout", json=random_order())
                log.info("POST /checkout -> %d", r.status_code)
        except httpx.HTTPError as exc:
            log.warning("request failed: %s", exc)
        time.sleep(random.uniform(0.3, 1.5))


if __name__ == "__main__":
    main()
