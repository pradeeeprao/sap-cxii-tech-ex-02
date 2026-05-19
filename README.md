# Technical Exercise — sap-cxii-tech-ex-02

## Goal

Build a data ETL microservice that ingests customer order data from CSV files, cleans/transforms the data, and exposes it via a simple query API. The emphasis is on data processing, system design, API development, and deployability — not on machine learning.

---

## Path by role

This exercise is used for two roles:

- **DS Expert** — complete Parts 1, 2, 3. The Bonus section is optional.
- **AI Architect** — complete Parts 1, 2, 3, **and Part 4 (AI-augmented query layer + architectural extension, below)**. Skip the Bonus.

Time budget:
- DS Expert: 3–4 hours
- AI Architect: 5–6 hours (includes Part 4)

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

## Part 4 — AI-Augmented Query Layer + Architectural Extension (AI Architect only)

DS Experts: skip this section and pursue the Bonus items instead.

---

### Part 4a — Natural Language Query Endpoint (hands-on, required)

Extend your API with a new endpoint:

```
POST /orders/ask
Content-Type: application/json

{"question": "What is the total revenue from customer C001 in the last 30 days?"}

→ {"answer": "Total revenue: $4,230.00 (3 orders)", "sql_used": "SELECT ...", "rows": [...]}
```

Use an LLM of your choice (OpenAI, Anthropic, a local Ollama model — justify your pick in the README) to convert the natural language question into SQL, execute it against your existing SQLite/Parquet store, and return both the natural-language answer and the SQL used.

**Requirements:**

- The LLM must receive the database schema as context in its system prompt (column names and types from Part 1).
- If the generated SQL is invalid or returns a runtime error, retry **once** with the error message appended to the prompt — implement this retry loop explicitly.
- Return `400` with a clear message if the question cannot be answered from the available schema (e.g. asks about product categories that do not exist).
- Log the prompt, generated SQL, and token count for each request.

**In your README, document:**
- Which model and provider you chose and why.
- The system prompt template you used (paste it).
- One example where the retry loop fired: what the bad SQL was, what error it produced, and what the corrected SQL looked like.

---

### Part 4b — Semantic Order Search (hands-on, required)

Add a second new endpoint:

```
GET /orders/semantic_search?q=high+value+recent+orders&top_k=5
→ [{"order_id": "...", "customer_id": "...", "amount_usd": 320.0, "order_date": "2024-03-15", "score": 0.91}, ...]
```

**Implementation:**

- At service startup, embed each order record as a short text string (e.g. `"customer C001, $320 USD, 2024-03-15"`) using `sentence-transformers` — name the model you chose and justify it.
- Store embeddings in a FAISS index (or in-memory numpy — explain the trade-off).
- At query time, embed the free-text query with the same model and return the top-k nearest orders by cosine similarity.
- The index must rebuild automatically when `etl.py` loads new data.

**In your README, document:**
- The embedding model and why it suits short structured-text records.
- How you handle index rebuilds without blocking in-flight requests (or acknowledge the gap if you do not).

---

### Part 4c — Bonus: LangGraph Agent

Replace the single-shot LLM call in Part 4a with a two-node LangGraph agent:

- **Node `sql_writer`:** generates SQL from the question + schema.
- **Node `sql_executor`:** runs the SQL; on failure routes back to `sql_writer` with the error appended (up to 2 retries), then routes to `END` on success.

Show the graph definition and include a trace of one multi-hop execution in your README: question → bad SQL → error → corrected SQL → answer.

---

### Part 4d — Architectural Extension (write-up, ≤ 1 page, required)

You now have a service with three AI components: an LLM call, an embedding model, and a vector index. Scale it to **50 enterprise customers**, each with their own data residency requirement (EU in eu-west, US in us-east, KSA on local cloud).

Address the following — diagrams welcome:

1. **Tenant isolation for the vector index** — one shared FAISS index with namespace filtering, or one index per tenant? What are the memory, latency, and data-leakage trade-offs?
2. **LLM backend per tenant** — some enterprise customers will require an on-premise model (e.g. a private Llama deployment) rather than a cloud API. Where in the stack does that routing live, and how do you keep the prompt template layer model-agnostic?
3. **PII in the NL→SQL pipeline** — order data contains customer IDs and amounts. What guardrails do you add before the question and schema reach the LLM, and does your answer change if the LLM is a third-party cloud API vs. on-premise?
4. **One specific decision** — pick the highest-leverage architectural choice you made above and state the trade-off you accepted.

We are NOT asking you to implement the multi-tenant design. We are looking for architect-grade reasoning in writing.

---

## Bonus (Optional — DS Expert path only)

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
- Any Python libraries may be used (e.g., `pandas`, `sqlite3`, `fastapi`, `uvicorn`, `sentence-transformers`, `faiss-cpu`, `langchain`, `langgraph`, etc.).

---

## Deliverables

Code in a GitHub repo or zip file with:

- `etl.py`
- `app.py` (FastAPI service)
- `Dockerfile`
- `k8s/` folder with manifests
- `README.md` with setup instructions, design notes, and Part 4 write-up
