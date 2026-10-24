"""Stream NYC yellow-taxi trips into a MongoDB database as nested documents.

    .venv/bin/python -m benchmarks.import_taxi_to_mongo "mongodb+srv://…" --trips 300000

Reads the public January 2024 Parquet file over HTTP with DuckDB and inserts in
batches, so nothing is written to local disk. Creates (replacing) the
collections ``trips`` and ``zones`` in the database ``argus_demo``. Stops
early if the cluster's storage nears --max-mb (the Atlas free tier has 512 MB).
"""

from __future__ import annotations

import argparse
import time

import duckdb
from pymongo import MongoClient

TRIPS_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet"
ZONES_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"
PAYMENT = {1: "card", 2: "cash", 3: "no charge", 4: "dispute", 5: "unknown", 6: "voided"}


def storage_mb(client: MongoClient) -> float:
    total = 0.0
    for name in client.list_database_names():
        if name in ("admin", "local", "config"):
            continue
        stats = client[name].command("dbStats")
        total += stats.get("storageSize", 0) + stats.get("indexSize", 0)
    return total / 1e6


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("uri")
    parser.add_argument("--trips", type=int, default=300_000)
    parser.add_argument("--max-mb", type=float, default=480)
    parser.add_argument("--batch", type=int, default=5_000)
    args = parser.parse_args()

    client = MongoClient(args.uri, serverSelectionTimeoutMS=15_000)
    used = storage_mb(client)
    print(f"Cluster storage in use before import: {used:.0f} MB", flush=True)
    database = client["argus_demo"]
    database.drop_collection("trips")
    database.drop_collection("zones")

    conn = duckdb.connect()
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    zones = conn.execute(f"SELECT * FROM read_csv_auto('{ZONES_URL}')").fetchall()
    database.zones.insert_many(
        [{"zone_id": int(z[0]), "borough": z[1], "zone": z[2], "service_zone": z[3]} for z in zones]
    )
    print(f"zones: {len(zones)} documents", flush=True)

    cursor = conn.execute(
        f"""SELECT tpep_pickup_datetime, tpep_dropoff_datetime, passenger_count, trip_distance,
                   PULocationID, DOLocationID, payment_type, fare_amount, tip_amount, tolls_amount,
                   total_amount, congestion_surcharge, Airport_fee
            FROM read_parquet('{TRIPS_URL}') LIMIT {int(args.trips)}"""
    )
    inserted, start = 0, time.time()
    while True:
        rows = cursor.fetchmany(args.batch)
        if not rows:
            break
        documents = []
        for r in rows:
            doc = {
                "pickup": {"at": r[0], "zone_id": int(r[4])},
                "dropoff": {"at": r[1], "zone_id": int(r[5])},
                "distance_miles": float(r[3]),
                "payment": PAYMENT.get(int(r[6] or 5), "unknown"),
                "fare": {"base": float(r[7]), "tip": float(r[8]), "tolls": float(r[9]), "total": float(r[10])},
            }
            # Optional fields stay absent, as they would in a document store.
            if r[2] is not None:
                doc["passengers"] = int(r[2])
            if r[11]:
                doc["fare"]["congestion"] = float(r[11])
            if r[12]:
                doc["fare"]["airport"] = float(r[12])
            documents.append(doc)
        database.trips.insert_many(documents, ordered=False)
        inserted += len(documents)
        if inserted % 50_000 == 0:
            used = storage_mb(client)
            print(f"  {inserted:,} trips, {inserted / (time.time() - start):,.0f}/s, storage {used:.0f} MB", flush=True)
            if used > args.max_mb:
                print("Stopping: storage limit reached.", flush=True)
                break
    print(f"trips: {inserted:,} documents in {time.time() - start:.0f} s; storage {storage_mb(client):.0f} MB")


if __name__ == "__main__":
    main()
