# Technical Exercise — sap-cxii-tech-ex-02

## Goal

Build a data ETL microservice that ingests customer order data from CSV files, cleans/transforms the data, and exposes it via a simple query API. The emphasis is on data processing, system design, API development, and deployability — not on machine learning.

---

## Path by role

This exercise is used for two roles:

- **DS Expert** — complete Parts 1, 2, 3. The Bonus section is optional.
- **AI Architect** — complete Parts 1, 2, 3, **and Part 4 (architectural extension, below)**. Skip the Bonus.

Time budget:
- DS Expert: 3–4 hours
- AI Architect: 5–6 hours (includes Part 4 write-up)

---

## Dataset

You will be provided with one or more CSV files using the following schema:

```csv
order_id,customer_id,order_date,amount,currency
1001,C123,2020-01-01,200,USD
1002,C124,2020-01-02,150,EUR
...
```

**Notes:**
- `order_id` is unique.
- `customer_id` is an alphanumeric ID.
- `order_date` may have inconsistent formats (YYYY-MM-DD, MM/DD/YYYY, etc.).
- `amount` may contain invalid or missing values.
- `currency` may be USD, EUR, or missing.

---

## Part 1: ETL Pipeline

### Requirements

#### Extract
- Load raw CSV(s).

#### Transform
- Normalize dates into ISO 8601 format (`YYYY-MM-DD`).
- Convert all amounts into a single currency (e.g., USD) using fixed rates:
  - 1 EUR = 1.1 USD
  - 1 USD = 1 USD
- Handle missing/invalid values:
  - Drop rows with no `order_id` or `customer_id`.
  - For missing `amount` → set to 0.
  - For missing `currency` → assume USD.

#### Load
- Store cleaned data in either:
  - SQLite (table: `orders`), or
  - Parquet/CSV for quick retrieval.

### Deliverable

A script (`etl.py`) that runs:

```sh
python etl.py load data/orders.csv
```

This script should process and persist the cleaned dataset.

---

## Part 2: Query API (FastAPI)

### Requirements

Implement a FastAPI service with these endpoints:

- `GET /orders/customer/{customer_id}`  
  Returns all orders for a given customer.

- `GET /orders/stats`  
  Returns:
  - `total_revenue` (sum of amounts)
  - `avg_order_value`
  - `orders_per_day` (dict keyed by date)

- `GET /orders/recent?days=N`  
  Returns all orders from the last N days.

- `GET /healthz`  
  Returns `"ok"` for liveness.

#### Example response

```json
{
  "total_revenue": 12345.67,
  "avg_order_value": 87.5,
  "orders_per_day": {
    "2020-01-01": 15,
    "2020-01-02": 20
  }
}
```

---

## Part 3: Deployment

### Requirements

- **Dockerfile:**
  - Multi-stage build (builder → runtime).
  - Non-root user.
  - Expose port 8000.
  - Include healthcheck.

- **Kubernetes manifests:**
  - Deployment (with readiness/liveness probes).
  - Service (ClusterIP).
  - ConfigMap for configurable parameters (e.g., DB path).

---

## Part 4 — Architectural Extension (AI Architect only)

You have built a single-customer service. Now scale it to **50 enterprise customers**, each with their own data residency requirement (EU customers in eu-west, US in us-east, KSA on local cloud).

Write a ≤ 1-page architectural extension (diagrams welcome) covering:

1. **What stays, what splits** — which components are shared vs replicated per region/tenant?
2. **Data plane vs control plane** — where does config live, how do schema migrations propagate?
3. **Cost vs control trade-off** — N independent copies vs one shared multi-region cluster — what's your call and why?
4. **One specific decision** — pick the highest-leverage architectural choice and explain the trade-offs you weighed.

We are NOT asking you to implement this. We are looking for architect-grade reasoning in writing.

---

## Bonus (Optional)

### Metrics

- Expose `/metrics` in Prometheus format with counters (requests, errors, processing time).

### Caching

- Cache results of `/orders/stats` in memory (e.g., TTL = 60 seconds).

### CLI

- Add subcommands to `etl.py`:
  - `python etl.py show-stats` → print revenue/avg order.

---

## Assumptions

- CSVs are well-formed (one row per line).
- Order IDs are unique.
- Any Python libraries may be used (e.g., `pandas`, `sqlite3`, `fastapi`, `uvicorn`, etc.).

---

## Deliverables

Code in a GitHub repo or zip file with:

- `etl.py`
- `app.py` (FastAPI service)
- `Dockerfile`
- `k8s/` folder with manifests
- `README.md` with setup instructions and design notes
