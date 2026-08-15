# OpenTelemetry POC — Grafana Stack with Prometheus

A self-contained, end-to-end OpenTelemetry proof of concept for a simulated
**crypto trading environment**: a multi-venue market-data **feed handler**
pushes a consolidated book to a **trading engine** (plus an order-flow
generator), and all three emit **traces, metrics and logs** through an
**OpenTelemetry Collector** into the Grafana stack — with **Prometheus** as
the metrics backend (instead of Mimir).

```
 feed-handler ──pushes book──► trading-engine ◄──orders── loadgen
      │                              │
      └─────────────OTLP─────────────┴──► OTel Collector ──┬──► Tempo      (traces)
                                          (spanmetrics +   ├──► Loki       (logs)
                                           servicegraph)   └──► Prometheus (metrics,
                                                                remote write)
                                                Grafana reads from all three
```

## Demo applications

- **feed-handler** — simulates crypto market-data feeds from three venues
  (binance, coinbase, kraken) for BTC/USDT, ETH/USDT, SOL/USDT, XRP/USDT and
  DOGE/USDT: random-walk ticks per pair/venue, consolidated into a best
  bid/offer which is **pushed** to the trading engine once a second — the
  realistic publisher→subscriber direction (HTTP POST stands in for a
  WebSocket/multicast bus so trace context propagates). Emits per-batch
  publish spans, per-pair/venue tick counters, normalisation-latency
  histograms and occasional venue feed-gap warnings.
- **trading-engine** — order entry via `POST /orders`: pair validation,
  local **book lookup** (no network call on the order path — it executes
  against the cached book kept fresh by the feed pushes, recording a quote
  **staleness** histogram), a pre-trade **risk check** (max notional
  250k USD), then simulated execution routed to a venue with slippage.
  Failure modes: non-tradable pair (400), risk reject (409), insufficient
  liquidity (409), venue session lost (500) and slow executions.
- **loadgen** — continuous order flow across 20 accounts and all pairs,
  including delisted pairs (LUNA/USDT) and notional-breaching sizes so every
  signal (fill, 4xx reject, 5xx error) shows up in the data.

## Stack components

| Service        | Image                                        | Purpose                                        | Host port |
|----------------|----------------------------------------------|------------------------------------------------|-----------|
| trading-engine | built from `apps/trading-engine`             | Order entry; local book + risk + execution     | 8080      |
| feed-handler   | built from `apps/feed-handler`               | Multi-venue market-data simulation (publisher) | —         |
| loadgen        | built from `apps/loadgen`                    | Continuous order flow (incl. bad orders)       | —         |
| beyla-*        | grafana/beyla:2.2.0                          | eBPF auto-instrumentation sidecars (kernel-level HTTP metrics + L4 network flows, zero code) | — |
| ebpf-exporter  | ghcr.io/cloudflare/ebpf_exporter             | OS/kernel eBPF metrics: block I/O latency, syscalls, softirqs, page cache, TCP health, OOM kills; exec spans → Tempo | — |
| otel-collector | otel/opentelemetry-collector-contrib:0.116.1 | Central OTLP pipeline, RED + service-graph metrics | 4317, 4318 |
| prometheus     | prom/prometheus:v3.1.0                       | Metrics (OTLP → collector → remote write)      | 9092      |
| loki           | grafana/loki:3.3.2                           | Logs (native OTLP ingestion)                   | 3100      |
| tempo          | grafana/tempo:2.7.0                          | Traces                                         | 3200      |
| grafana        | grafana/grafana:11.5.1                       | Dashboards + Explore, fully provisioned        | 3001      |

> Ports 3001/9092 are used because 3000/9090 were already taken on the
> original host; adjust in `docker-compose.yml` if yours are free.

## Deployment

### Prerequisites

- **Linux host** — the eBPF components (Beyla, ebpf_exporter) need a modern
  kernel with BTF, ~5.14+; developed and verified on 6.17. Everything except
  the two eBPF layers also works on Docker Desktop (macOS/Windows), but the
  kernel dashboard will be empty there.
- **Docker Engine 24+** with the **Compose v2** plugin (`docker compose`,
  not `docker-compose`).
- Ability to run **privileged containers** (required by Beyla and
  ebpf_exporter to load eBPF programs).
- ~2 GB RAM headroom and ~5 GB disk for images + 7 days of telemetry.
- Free host ports: **3001** (Grafana), **9092** (Prometheus), **8080**
  (trading-engine), **3100** (Loki), **3200** (Tempo), **4317/4318** (OTLP).
  Remap in `docker-compose.yml` if any clash (the defaults already avoid
  3000/9090 for that reason).

### 1. Clone and configure

```bash
git clone https://github.com/willsc/OtelPOC.git
cd OtelPOC

# Optional: set Grafana admin credentials (defaults to admin/admin)
cat > .env <<'ENV'
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=change-me
ENV
```

### 2. Build and start

```bash
docker compose up -d --build
```

First run builds the three app images and pulls the stack images
(~2–4 min). Start order is handled by health checks: backends → collector →
trading-engine → feed-handler → loadgen.

### 3. Verify

```bash
# All 11 containers up; trading-engine and feed-handler "healthy"
docker compose ps

# Orders flowing (loadgen output)
docker compose logs -f loadgen

# Telemetry landing in each backend
curl -s 'http://localhost:9092/api/v1/query?query=sum(rate(traces_span_metrics_calls_total[1m]))'   # metrics
curl -s 'http://localhost:3200/api/search?limit=1'                                                  # traces
curl -sG 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service_name="trading-engine"}' --data-urlencode 'limit=1'               # logs

# Prometheus scrape targets all "up" (collector, tempo, loki, ebpf-exporter, itself)
curl -s http://localhost:9092/api/v1/targets | grep -o '"health":"[a-z]*"' | sort | uniq -c
```

Give it ~2 minutes after first start before expecting dashboard data
(span-metrics flush + first scrapes).

### 4. Use it

Open **Grafana: http://localhost:3001** (admin / admin unless overridden).

Everything is provisioned automatically:

- **Dashboards** (folder "OpenTelemetry POC"):
  - *Service Overview* — live service topology (node graph), request/error
    rates, latency percentiles, orders by outcome, per-pair tick rates,
    quote staleness at execution, live logs.
  - *eBPF (Beyla)* — kernel-observed HTTP metrics per route/status, outbound
    latency, body sizes, and an SDK-vs-eBPF latency comparison.
  - *Kernel eBPF (OS)* — host-level kernel telemetry from Cloudflare's
    ebpf_exporter: block I/O latency percentiles, top syscalls (+errors by
    errno), softirq service latency, page-cache ops, TCP retransmits and
    SYN backlog, plus Beyla L4 network-flow throughput.
  - *App Runtime* — per-service process metrics (memory, CPU, GC, threads,
    FDs) plus host CPU/memory.
  - *OpenTelemetry Collector — Health* — pipeline throughput, refusals,
    queue utilisation, batch sizes, collector process resources.
- **Explore → Tempo**: search traces, open the **Service Graph** tab
  (powered by the collector's servicegraph metrics in Prometheus) — the edge
  runs `feed-handler → trading-engine`, matching the real data-flow direction.
- **Correlations**: trace → logs (Loki), trace → metrics (Prometheus),
  log → trace via the `trace_id` structured-metadata link, metric exemplars → traces.

Submit an order yourself:

```bash
curl -s -X POST http://localhost:8080/orders \
  -H 'content-type: application/json' \
  -d '{"account_id": "ACC-0001", "pair": "BTC/USDT", "side": "buy", "amount": 0.25}'
```

### Operations

**Applying config changes** — the backend configs (`collector/`,
`prometheus/`, `loki/`, `tempo/`) are single-file bind mounts; editing them
replaces the file inode, so the container keeps seeing the old content until
restarted:

```bash
docker compose restart otel-collector    # after editing collector/config.yaml
docker compose restart prometheus        # after editing prometheus/prometheus.yml
```

App code or dependency changes need a rebuild:

```bash
docker compose up -d --build trading-engine feed-handler loadgen
```

Grafana dashboards/datasources under `grafana/` are picked up automatically
(the provisioner rescans every 30 s) — no restart needed.

**Upgrading component versions** — bump the pinned image tags in
`docker-compose.yml`, then `docker compose up -d`. Test collector upgrades
first: `docker compose run --rm otel-collector validate --config=/etc/otelcol/config.yaml`.

**Stopping and cleaning up**:

```bash
docker compose down             # stop; keeps telemetry data volumes
docker compose down -v          # stop AND delete all stored telemetry
docker compose down --rmi local # additionally remove built app images
```

**Troubleshooting**:

| Symptom | Check |
|---------|-------|
| Port already allocated on `up` | Another stack owns the host port — remap the left side of the `ports:` entry in `docker-compose.yml` |
| No metrics in dashboards | `docker compose logs otel-collector` for export errors; Prometheus targets page at http://localhost:9092/targets |
| Kernel dashboard empty | Host must be Linux with BTF (`ls /sys/kernel/btf/vmlinux`); `docker compose logs ebpf-exporter` for attach errors |
| ebpf-exporter restart-looping | A config in `--config.names` conflicts or can't attach on your kernel — see the exclusion notes in `docker-compose.yml` |
| Beyla exporting nothing | `docker compose logs beyla-trading-engine`; it needs the app container running first (`pid: service:` join) — `docker compose up -d` again re-creates it |
| 500s from /orders | Intentional ~3% simulated venue failures — that's the demo data |

## What proves it works

- `traces_span_metrics_*` and `traces_service_graph_*` series in Prometheus —
  spans are being received and converted to RED metrics by the collector;
  the service graph shows `feed-handler → trading-engine`.
- Custom app metrics (`orders_processed_total{outcome=...}`,
  `feed_ticks_processed_total{pair,venue}`, `trade_notional_USD_*`,
  `order_quote_staleness_seconds_*`) in Prometheus — OTLP metrics path works.
- `{service_name="trading-engine"}` in Loki returns structured logs carrying
  `trace_id` — OTLP logs path + log/trace correlation works.
- Tempo trace search shows cross-service `publish-feed-batch →
  POST /marketdata` traces (feed-handler into trading-engine) and
  single-service order traces (`POST /orders` → book lookup → risk check →
  execution), including error traces for rejects and venue failures.
- `http_server_request_duration_seconds*{telemetry_sdk_name="beyla"}` in
  Prometheus — kernel-level eBPF metrics captured with zero code changes;
  the eBPF dashboard overlays SDK vs eBPF p95 latency for the same service,
  and the kernel view also sees what the SDK excludes (e.g. `/healthz`).

## Design decisions

- **Feed handler publishes, engine subscribes**: market data is *pushed*
  downstream and the trading engine executes against a local book — an order
  never blocks on a network quote lookup, mirroring real trading systems.
  The cost is that order traces are single-service; the cross-service traces
  live on the market-data path, and the quote-staleness histogram covers the
  freshness risk this pattern introduces.
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
  plus manual custom spans (book lookup, risk check, execution, feed batches),
  counters and histograms — demonstrating both approaches. `/healthz` is
  excluded from tracing via `OTEL_PYTHON_EXCLUDED_URLS`.

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
