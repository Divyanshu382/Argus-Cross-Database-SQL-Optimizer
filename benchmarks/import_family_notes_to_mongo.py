"""Create a MongoDB collection of curation notes, one per Rfam family.

    .venv/bin/python -m benchmarks.import_family_notes_to_mongo "mongodb+srv://…"

The demo joins three databases on the Rfam family accession: Rfam's
``full_region`` (MySQL), RNAcentral's ``rfam_models`` (PostgreSQL) and this
collection (MongoDB). The accessions come from the public Rfam database; the
curation fields are generated (seeded, so every run gives the same documents)
and stand in for a team's own notes. Creates (replacing) the collection
``family_notes`` in the database ``argus_demo``.
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta

import mysql.connector
from pymongo import MongoClient

TEAMS = ["Structure", "Genomics", "Evolution", "Annotation"]
REVIEWERS = ["asha", "ben", "chen", "dara", "eli", "fatima", "goran", "hana"]
TAGS = ["conserved", "needs-structure", "bacterial", "viral", "human", "seed-update", "literature"]


def families() -> list[tuple[str, str, str]]:
    conn = mysql.connector.connect(
        host="mysql-rfam-public.ebi.ac.uk", port=4497, user="rfamro", database="Rfam", connection_timeout=30
    )
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT rfam_acc, rfam_id, type FROM family ORDER BY rfam_acc")
        return cursor.fetchall()
    finally:
        conn.close()


def note(rng: random.Random, acc: str, rfam_id: str, kind: str) -> dict:
    status = rng.choices(["reviewed", "in_progress", "flagged"], weights=[6, 3, 1])[0]
    return {
        "rfam_acc": acc,
        "rfam_id": rfam_id,
        "curation": {
            "status": status,
            "team": rng.choice(TEAMS),
            "reviewer": rng.choice(REVIEWERS),
            "reviewed_on": datetime(2024, 1, 1) + timedelta(days=rng.randrange(600)),
        },
        "priority": rng.choice([1, 2, 3]),
        "tags": rng.sample(TAGS, rng.randrange(0, 3)),
        "rfam_type": kind,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("uri")
    args = parser.parse_args()

    try:
        import certifi

        tls = {"tlsCAFile": certifi.where()}
    except ImportError:
        tls = {}
    client = MongoClient(args.uri, serverSelectionTimeoutMS=15_000, **tls)
    rows = families()
    rng = random.Random(42)
    docs = [note(rng, *row) for row in rows]
    collection = client["argus_demo"]["family_notes"]
    collection.drop()
    collection.insert_many(docs)
    collection.create_index("rfam_acc", unique=True)
    reviewed = sum(d["curation"]["status"] == "reviewed" for d in docs)
    print(f"argus_demo.family_notes: {len(docs):,} documents ({reviewed:,} reviewed)")


if __name__ == "__main__":
    main()
