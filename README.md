# OpenTelemetry POC — Grafana Stack with Prometheus

A self-contained, end-to-end OpenTelemetry proof of concept: a simulated
**trading engine** and **market-data feed handler** (plus an order-flow
generator) feed **traces, metrics and logs** through an **OpenTelemetry
Collector** into the Grafana stack — with **Prometheus** as the metrics
backend (instead of Mimir).

```
                                              ┌────────────► Tempo ────────┐  traces
 loadgen ──► trading-engine ──► feed-handler  │                            │
 (orders)         │                  │        ├────────────► Loki ─────────┤  logs
                  └───────OTLP───────┴──► OTel Collector                   ├──► Grafana
                                              │  (spanmetrics +           │
                                              │   servicegraph)           │
                                              └─remote write─► Prometheus ┘  metrics
```

## Demo applications

- **feed-handler** — simulates a market-data feed: a background task applies
  random-walk ticks for a 5-symbol universe (AAPL, MSFT, NVDA, AMZN, TSLA),
  maintains top-of-book quotes and serves them via `GET /quote/{symbol}`.
  Emits per-batch feed spans, tick counters and processing-latency
  histograms; occasionally logs feed-gap resyncs and takes a slow
  "rebuild from depth" path.
- **trading-engine** — order entry via `POST /orders`: symbol validation,
  a pre-trade risk check (max quantity), quote lookup from the feed handler,
  then simulated execution with slippage. Realistic failure modes: symbol not
  tradable (400), risk rejects (409), insufficient liquidity (409), venue
  errors (500) and slow executions.
- **loadgen** — continuous order flow across accounts/symbols, including
  deliberately non-tradable symbols and risk-breaching quantities so every
  signal (fill, 4xx reject, 5xx error) shows up in the data.

## Stack components

| Service        | Image                                        | Purpose                                        | Host port |
|----------------|----------------------------------------------|------------------------------------------------|-----------|
| trading-engine | built from `apps/trading-engine`             | Order entry; risk check + execution            | 8080      |
| feed-handler   | built from `apps/feed-handler`               | Market-data simulation + quote API             | —         |
| loadgen        | built from `apps/loadgen`                    | Continuous order flow (incl. bad orders)       | —         |
| otel-collector | otel/opentelemetry-collector-contrib:0.116.1 | Central OTLP pipeline, RED + service-graph metrics | 4317, 4318 |
| prometheus     | prom/prometheus:v3.1.0                       | Metrics (OTLP → collector → remote write)      | 9092      |
| loki           | grafana/loki:3.3.2                           | Logs (native OTLP ingestion)                   | 3100      |
| tempo          | grafana/tempo:2.7.0                          | Traces                                         | 3200      |
| grafana        | grafana/grafana:11.5.1                       | Dashboards + Explore, fully provisioned        | 3001      |

> Ports 3001/9092 are used because 3000/9090 were already taken on the
> original host; adjust in `docker-compose.yml` if yours are free.

## Quick start

```bash
docker compose up -d --build
```

Then open **Grafana: http://localhost:3001** (admin / admin — override with
`GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` in a `.env` file).

Everything is provisioned automatically:

- **Dashboard**: *OpenTelemetry POC — Service Overview* (folder "OpenTelemetry POC"):
  request rate, error rate, latency percentiles, orders by outcome,
  market-data tick rates, live logs.
- **Explore → Tempo**: search traces, open the **Service Graph** tab
  (powered by the collector's servicegraph metrics in Prometheus).
- **Correlations**: trace → logs (Loki), trace → metrics (Prometheus),
  log → trace via the `trace_id` structured-metadata link, metric exemplars → traces.

Submit an order yourself:

```bash
curl -s -X POST http://localhost:8080/orders \
  -H 'content-type: application/json' \
  -d '{"account_id": "ACC-0001", "symbol": "NVDA", "side": "buy", "qty": 100}'
```

## What proves it works

- `traces_span_metrics_*` and `traces_service_graph_*` series in Prometheus —
  spans are being received and converted to RED metrics by the collector.
- Custom app metrics (`orders_processed_total{outcome=...}`,
  `feed_ticks_processed_total{symbol=...}`, `trade_notional_USD_*`,
  `order_execution_duration_seconds_*`) in Prometheus — OTLP metrics path works.
- `{service_name="trading-engine"}` in Loki returns structured logs carrying
  `trace_id` — OTLP logs path + log/trace correlation works.
- Tempo trace search shows `trading-engine → feed-handler` traces (order →
  risk check → quote → execution), including error traces for risk rejects
  and venue failures, plus standalone `process-feed-batch` traces from the
  feed handler's background loop.

## Design decisions

- **Collector-centric**: apps only speak OTLP to one endpoint; backends can be
  swapped without touching application code. This is the pattern you would
  keep in production (as a DaemonSet/sidecar + gateway tier on Kubernetes).
- **Prometheus instead of Mimir**: the collector pushes via
  `prometheusremotewrite` to Prometheus's remote-write receiver
  (`--web.enable-remote-write-receiver`). Migrating to Mimir later is a
  one-line endpoint change in `collector/config.yaml`.
- **RED metrics from spans**: the `spanmetrics` connector generates
  rate/error/duration metrics, `servicegraph` generates the service-map edges
  — no Tempo metrics-generator needed.
- **`resource_to_telemetry_conversion`** promotes `service.name` etc. to
  metric labels for simple dashboard queries; `target_info` is disabled since
  it becomes redundant (and would be double-written by the two metric
  pipelines). Review cardinality before doing this on a large fleet.
- **Auto + manual instrumentation**: the Python apps use
  `opentelemetry-instrument` for FastAPI/httpx/logging auto-instrumentation,
  plus manual custom spans (risk check, execution, feed batches), counters
  and histograms — demonstrating both approaches. `/healthz` is excluded
  from tracing via `OTEL_PYTHON_EXCLUDED_URLS`.

## Production hardening checklist (beyond POC scope)

- TLS + authentication on all OTLP and backend endpoints (this POC runs
  plaintext on an isolated Docker network).
- Object storage (S3/GCS) for Loki and Tempo instead of local filesystem;
  HA/replicated deployments.
- Prometheus long-term storage strategy (or that's where Mimir/Thanos come in).
- Tail-based sampling in the collector once volume grows
  (`tailsamplingprocessor`).
- Change the Grafana admin password, wire up SSO, and set alerting rules.
- Collector deployed in two tiers (agent + gateway) with queued retry and
  a persistent sending queue.

## Repo layout

```
apps/                 # trading-engine, feed-handler (FastAPI) + order-flow loadgen
collector/config.yaml # OTel Collector pipelines
prometheus/           # scrape config (self-monitoring only; app metrics are pushed)
loki/ tempo/          # single-binary backend configs
grafana/provisioning/ # datasources (with correlations) + dashboard provider
grafana/dashboards/   # dashboard JSON
docker-compose.yml
```
