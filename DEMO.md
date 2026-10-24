# Demo: one query across MySQL, PostgreSQL and MongoDB

One SQL query joins three databases on three different servers:

| Name in Argus | Database | Where | What it holds |
|---|---|---|---|
| `rfam` | MySQL | EMBL-EBI public server (UK) | `full_region`: ~10.6 million RNA regions found in genomes |
| `rnacentral` | PostgreSQL | EMBL-EBI public server (UK) | `rfam_models`: 4,252 RNA family models with their RNA type |
| `mongo` | MongoDB | Atlas (cloud) | `family_notes`: 4,227 curation documents, one per family, with nested `curation.status/team/reviewer` |

They link on the Rfam family accession (`rfam_acc` = `rfam_model_id`).
`family_notes` was created with `benchmarks/import_family_notes_to_mongo.py`
(accessions are real; the curation fields are generated stand-ins for a team's own notes).

## The query

```sql
SELECT m.rna_type, COUNT(*) AS regions, AVG(r.bit_score) AS avg_bit_score
FROM rfam.full_region r
JOIN rnacentral.rfam_models m ON m.rfam_model_id = r.rfam_acc
JOIN mongo.family_notes n ON n.rfam_acc = r.rfam_acc
WHERE n.curation_status = 'reviewed'
GROUP BY m.rna_type
ORDER BY regions DESC
LIMIT 8
```

Measured on 24 Oct 2026:

| | Time | Notes |
|---|---|---|
| Normal (exact) | **4 min 1 s** | pulls 10.6M rows from the MySQL server, joins in DuckDB |
| Argus estimate | **13–28 ms** | stored 1% sample of `full_region` + local copies of the two small tables |
| Speedup | **~8,700×** | |
| Accuracy | **0.87% mean error** | all 16 values inside their 95% intervals; largest error 2.9% |

## Before the demo (morning of)

1. **Atlas network access.** If you present on a different Wi-Fi, Atlas will refuse the
   connection. Atlas → Security → Network Access → *Add Current IP Address* from the demo
   network (or add `0.0.0.0/0` for the day and remove it afterwards).
2. Start both servers:
   ```bash
   cd Argus && .venv/bin/python -m uvicorn backend.main:app --port 8093
   cd Argus/frontend && npm run dev
   ```
   Open http://localhost:5173.
3. Keep your Atlas connection string ready (not stored in this repo):
   `mongodb+srv://<user>:<password>@cluster0.848lkgn.mongodb.net/argus_demo`
4. Do one dry run of the steps below. Then click **Clear cache** in the Timing panel so the
   live run measures real times (a repeat would otherwise show "from cache").

The stored samples (`rfam.full_region` 1%, local copies of `rnacentral.rfam_models` and
`mongo.family_notes`) live on disk and survive restarts. Connections do not: add them again
after every backend restart.

## Live steps (about 6 minutes)

1. **Add the three databases.** *Add source* →
   - Rfam (MySQL), one click (~12 s)
   - RNAcentral (PostgreSQL), one click (~20 s)
   - MongoDB → paste the Atlas string, database `argus_demo`, name it `mongo` (~2 s)

   The sidebar now shows `rfam` (MY), `rnacentral` (PG) and `mongo` (MongoDB).
2. **Paste the query.** Point out that each table lives in a different database.
3. In Timing, tick **time the normal query after an estimate**.
4. Click **Estimate**. The answer appears in ~20 ms, with ± intervals and the cost model's
   prediction (it recommends the estimate, ~300× faster).
5. While the normal query runs in the background (~4 min), explain:
   - DuckDB is the hub: MySQL and PostgreSQL are attached read-only, MongoDB is fetched
     with only the fields the query uses, with filters run inside MongoDB.
   - The estimate reads a 1% stored sample of the 10.6M-row table and joins it with local
     copies of the small tables, so it never waits on the network.
   - Every value has a 95% interval: the true value lies inside it 19 times out of 20.
6. When the normal query finishes, the tiles fill in: **~8,700× faster, <1% off**. Show:
   - the Exact / Estimate / Error columns for each value,
   - "How the cost model helped": it recommended the estimate and was right,
   - Run details: the sample the estimate read.

## If something goes wrong

- **No time to wait 4 minutes:** run this instead with **Run both and compare** (one table on
  the MySQL server; measured 6.2 s normal vs 7 ms estimate, 925× faster, 0.5% off):
  ```sql
  SELECT COUNT(*), SUM(bit_score), AVG(bit_score) FROM rfam.full_region
  ```
- **Atlas fails to connect:** network access (step 1 above), or the password changed.
- **EBI servers slow or down:** they are public; show the numbers in the table above.
- **The cost model's normal-time prediction is far off (8.7 s vs 4 min):** it has rarely seen
  three-database joins that pull millions of rows. It still made the right call; say so.
