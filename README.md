# Argus

Argus answers SQL aggregate queries over local files and connected databases (PostgreSQL, MySQL, MongoDB) in two ways: **exactly**, and **approximately** from a random sample, with a 95% confidence interval on every estimate. It runs both side by side so the speedup and the error can be measured rather than assumed.

```
                     Browser (React + Vite, frontend/)
                                  │
                       FastAPI backend (backend/)
    ┌─────────────────────────────┼──────────────────────────────┐
 exact_engine                approx_engine                   plan / samples /
 (whole query sent to        single table: runtime_sampling   connections APIs
  its source if possible)    joins: federation (sqlglot)
    └─────────────────────────────┼──────────────────────────────┘
                     DuckDB (one shared connection)
     ┌────────────┬───────────────┼───────────────┬──────────────┐
  local tables  Parquet/CSV    PostgreSQL       MySQL         MongoDB
  & uploads     URLs           (ATTACH)         (ATTACH)      (snapshot)
                                  │
                   aq_samples: stored samples of remote tables
```

## What it does

- **Exact queries** run in DuckDB. When every table in a query lives in one connected database, the whole query is translated to that database's SQL dialect with [sqlglot](https://github.com/tobymao/sqlglot) and run there, so only the result crosses the network (`ILIKE` becomes `LOWER(…) LIKE LOWER(…)` for MySQL, for example). If the source rejects it, the query runs in DuckDB instead.
- **Approximate queries** support `COUNT`, `SUM` and `AVG` with `WHERE`, `GROUP BY`, `ORDER BY`, `LIMIT` and inner joins, including joins across databases. Each estimate comes with a 95% confidence interval, and the sampler stops as soon as every interval is within the chosen margin (Fast ±20%, Balanced ±8%, Accurate ±2%).
- **Connected databases**: PostgreSQL and MySQL are attached read-only and queried in place. MongoDB collections are queried live with SQL: a query over one collection is translated to an aggregation pipeline and runs inside MongoDB; joins and anything MongoDB cannot express fetch only the fields they use, with filters applied in MongoDB. Nested fields are flattened (`pickup.zone_id` → `pickup_zone_id`). Two public EMBL-EBI databases (Rfam on MySQL, RNAcentral on PostgreSQL) can be connected in one click to try it.
- **Data from anywhere else**: upload a CSV (deduplicated by content hash) or load a Parquet/CSV/JSON URL as a remote view or a local table.
- **Query suggestions** ([`backend/core/suggest.py`](backend/core/suggest.py)): pick a table and Argus profiles a sample of it (categories, measures, dates, keys) and suggests the questions those shapes invite, including joins with tables in any connected source that share a key column, each tagged with the cost model's speed prediction.
- **Normal time alongside every estimate**: an estimate appears first, the normal (exact) query keeps running in the background, and its time, the speedup and the accuracy fill in when it finishes.
- **One workbench screen**: sources on the left (local tables and every connected database, with their tables and columns), an SQL editor, and results that put each exact value next to its estimate, its 95% interval and how far off it was. Tabs show the SQL or MongoDB pipeline that actually ran, the execution plan as a graph, and query history. Light and dark themes.

## How the estimates work

Every sample is Poisson sampling: each *unit* is kept independently with probability *f*, and `COUNT`/`SUM` are scaled by 1/*f* (Horvitz-Thompson). The variance is estimated from the same sample, giving the interval estimate ± 1.96·SE. `AVG` is a ratio estimator with a linearised variance. The formulas are in [`backend/core/estimators.py`](backend/core/estimators.py).

| Data | How it is sampled | Unit |
|---|---|---|
| Local table ≥ 204,800 rows | DuckDB block sampling (`USING SAMPLE … (system)`) | 2048-row block: blocks are what gets kept or dropped, and rows in a block are correlated, so treating rows as units would understate the error |
| Smaller local tables, views | Row sampling (`bernoulli`) | Row |
| MongoDB collection | A live `$sample` per step (served by a random cursor, not a scan, below 5% of the collection), or a stored sample | Row |
| Table in a connected database | A **stored sample** pulled once and kept locally (see below) | Row |
| Inner join | The largest table is sampled and joined with the others whole | A row (or block) of the sampled table: each joined row comes from exactly one of its rows, which keeps COUNT/SUM unbiased for joins of any shape |

**How large a sample.** Sampling is two-phase: a small pilot sample measures the variance and is never reported; the answer comes from a second, independent sample sized from it so every 95% interval lands within the chosen margin (the width shrinks with √((1−f)/f)). Reporting the pilot whenever its interval happened to look narrow would favour samples that missed the outliers, so it is never used directly. Heavy-tailed data also gets a minimum size from Cochran's rule (at least 25·g₁² values, g₁ the skewness), below which the normal approximation behind the intervals is unreliable. On a heavy-tailed filtered average these two changes raised coverage from about 60% to about 95%.

**Why stored samples instead of sampling on the source.** It does not save time there: MySQL has no sampling (`RAND() < 0.01` still read all 10.6M rows and took longer than the exact query), and PostgreSQL `TABLESAMPLE SYSTEM (1)` on a 61M-row table took 466 s on the shared public server. So a random sample (`RAND()` / `TABLESAMPLE BERNOULLI`) is pulled once into the `aq_samples` schema of the local DuckDB file, and estimates come from it without contacting the source, even after a restart. A "sample" of 100% is a full local copy, meant for small tables that big ones are joined with. Samples are snapshots: rebuild them after the source changes.

## Learned cost model

Before a query runs, Argus predicts how long the exact query and the estimate will take, and recommends the faster one (the prediction line under the editor; `POST /api/advise`). Sampling is not always worth it: on small local tables DuckDB's exact answer is already a few milliseconds, while on a remote 10M-row table an estimate is thousands of times faster.

- **Features** ([`backend/ml/features.py`](backend/ml/features.py)), measured without running the query: table sizes (local counts, MySQL `table_rows`, Postgres `reltuples`, MongoDB estimated counts, cached), where each table lives, joins, groups, conditions, stored samples and local copies, network latency to the source, whether the whole query can be pushed to its source, block vs row sampling, and the accuracy target.
- **Pilot features**: `advise` also runs the sampler's first, millisecond-cheap step and records its time, interval width, skewness requirement and group count, plus the sample fraction the target will need. These describe the data itself, not just its size.
- **Model** ([`backend/ml/model.py`](backend/ml/model.py)): two gradient-boosted regressors (scikit-learn) predicting log10 seconds, the exact one from size and shape features and the estimate one also from the pilot; the estimate is recommended when the predicted speedup is at least 1.5×. Without a trained model, a hand-written rule decides. Repeated runs of a query are merged into their median before training.
- **Data**: every query run through the app is logged with its features and times (`datasets/ml_runs.jsonl`); [`benchmarks/generate_training_data.py`](benchmarks/generate_training_data.py) generates more across synthetic tables (20k to 6M rows), TPC-H and the public Rfam database.
- **Evaluation** ([`backend/ml/train.py`](backend/ml/train.py)): leave-one-dataset-out, so every prediction is for tables the model never saw; compared with the rule, "always exact", "always estimate" and an oracle. Results: [`benchmarks/ML_RESULTS.md`](benchmarks/ML_RESULTS.md).

```bash
.venv/bin/python -m benchmarks.generate_training_data --remote   # collect runs
.venv/bin/python -m backend.ml.train                             # evaluate, save backend/ml/data/cost_model.joblib
```

## Results

Every kind of source side by side (local table, imported and raw CSV, Parquet local and over HTTP, MySQL, PostgreSQL, MySQL × PostgreSQL joins) is in [`benchmarks/SOURCES_RESULTS.md`](benchmarks/SOURCES_RESULTS.md) (`.venv/bin/python -m benchmarks.benchmark_sources`): estimates on connected databases ran 380–5,600× faster than the normal query, mostly under 2% error; on local data the normal query was already as fast or faster. Earlier, more detailed runs are in [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md). In short:

- **Remote data is where it pays.** On the public Rfam MySQL server, a 10.6M-row aggregate took 61 s exactly and 14 ms from a 1% stored sample (0.86% mean error). A join of that table with `family` took 67 s exactly; approximately it took 3.4 s while `family` was still read over the network, and 38 ms once `family` had a full local copy (1.2% mean error).
- **Local data gains little.** DuckDB answers exact queries on 3M TPC-H rows in 2–38 ms, so sampling is at best about 2× faster and often slower.
- **The intervals are honest.** Across 30 runs per query, 93–100% of exact values fell inside their 95% intervals. Each preset reaches its margin when that takes at most half the data (Fast: about 9% of rows for ±17%; Balanced: about 37% for ±7%); Accurate (±2%) would need more on block-sampled TPC-H data, so it stops at 50% (±5%) and reports that an exact query is the better choice.

## Run it

Requires Python 3.12 and Node 20+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # backend + test dependencies

# backend, from the project root
.venv/bin/python -m uvicorn backend.main:app --reload --port 8093

# frontend, in another terminal
cd frontend && npm install && npm run dev        # http://localhost:5173
```

API docs are at http://127.0.0.1:8093/docs.

```bash
.venv/bin/pytest tests                                   # offline test suite (~1 s)
.venv/bin/python -m benchmarks.run_benchmarks --local    # local benchmarks (~1 min)
.venv/bin/python -m benchmarks.run_benchmarks            # + remote (needs network, ~15 min)
```

The DuckDB file lives at `datasets/aetherquery.duckdb` (override with `AETHERQUERY_DUCKDB_PATH`). PostgreSQL/MySQL configured by environment variables (`PGHOST`, `MYSQL_HOST`, …) appear as the "configured on the server" sources; databases connected from the UI do not need them.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/execute` | Run a query. `mode`: `exact`, `approx`/`balanced`, `fast`, `accurate`, or `benchmark` (both, plus speedup, error and interval coverage). Optional `accuracy_target` (e.g. 92 = stop at ±8%). |
| `GET /api/execute/progress/{id}` | Live progress of a running query |
| `POST /api/sql/parse-plan` | Execution plan as a tree |
| `POST /api/upload` | Upload a CSV |
| `POST /api/load-url`, `GET /api/datasets/public` | Load a Parquet/CSV/JSON URL; list presets |
| `GET/POST /api/connections`, `DELETE /api/connections/{name}` | Connect PostgreSQL, MySQL or MongoDB (by URI or fields) |
| `GET /api/connections/public`, `POST /api/connections/public/{name}` | Public demo databases |
| `GET/POST/DELETE /api/samples`, `GET /api/samples/lookup` | Stored samples and full local copies |
| `GET /api/columns?table=` | Columns of a (qualified) table |

## Limitations

- Approximation covers `COUNT`, `SUM` and `AVG` only. `MIN`, `MAX`, `COUNT(DISTINCT)`, `HAVING`, outer joins, subqueries and window functions run exactly.
- Intervals rely on the normal approximation; with few sampled rows or blocks, coverage runs a little under 95% (measured in the benchmarks). Groups too rare to appear in the sample are missing from the estimate.
- Stored samples do not track changes on the source.
- MongoDB column types are inferred from up to 1,000 sampled documents per collection; a field whose type varies is read as text. Arrays are JSON text, so they can be filtered as text but not unnested.
- Connections (and their credentials) are kept in memory only and must be made again after a restart. Stored samples persist.
- On PostgreSQL servers that deny catalog access, tables are read through plain `SELECT`s, so filters are applied after the table is downloaded.

## Layout

```
backend/
  api/            HTTP routes (execute, plan, upload, connections, samples)
  core/
    estimators.py      estimates and confidence intervals from a sample
    runtime_sampling.py  sampling ladder for single tables
    federation.py      join estimates and whole-query pushdown (sqlglot)
    mongo_query.py     SQL -> MongoDB aggregation pipeline
    exact_engine.py, approx_engine.py, benchmark.py, parser.py, plan_parser.py
  db/
    duckdb.py          shared DuckDB connection
    connections.py     PostgreSQL/MySQL/MongoDB registry
    mongo.py           live MongoDB collections (schema, fetching, sampling)
    samples.py         stored samples
  ml/                learned cost model: features, run log, model, training
frontend/src/        React app: App.tsx (workbench), components/ (sources, editor, results, dialogs)
benchmarks/          benchmark harness and results
tests/               pytest suite (offline)
oldcodes/            earlier prototypes, kept for reference
```
