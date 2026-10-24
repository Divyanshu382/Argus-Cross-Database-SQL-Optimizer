import hashlib
import re
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.db.duckdb import (
    create_table_from_csv,
    create_table_from_url,
    describe_table,
    list_local_tables,
    table_exists,
)

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parents[2]
_UPLOAD_DIR = _BASE_DIR / "datasets"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_CHUNK_SIZE = 1024 * 1024

# Public datasets that can be queried straight from the internet.
PUBLIC_DATASETS = [
    {
        "name": "lineitem",
        "label": "TPC-H lineitem (SF1, ~6M rows, 273 MB)",
        "url": "https://blobs.duckdb.org/data/tpch-sf1/lineitem.parquet",
    },
    {
        "name": "lineitem_tiny",
        "label": "TPC-H lineitem (SF0.01, ~60K rows)",
        "url": "https://shell.duckdb.org/data/tpch/0_01/parquet/lineitem.parquet",
    },
    {
        "name": "yellow_taxi",
        "label": "NYC yellow taxi trips, Jan 2024 (~3M rows, 50 MB)",
        "url": "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet",
    },
]


@router.post("/upload")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported")

    # Stream to a temp file while hashing, then name the file and table by
    # content so uploading the same CSV again reuses the existing copy.
    temp_path = _UPLOAD_DIR / f".upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    try:
        with temp_path.open("wb") as buffer:
            while chunk := await file.read(_CHUNK_SIZE):
                digest.update(chunk)
                buffer.write(chunk)
        content_id = digest.hexdigest()[:32]
        final_path = _UPLOAD_DIR / f"{content_id}.csv"
        if final_path.exists():
            temp_path.unlink()
        else:
            temp_path.rename(final_path)
    finally:
        temp_path.unlink(missing_ok=True)

    # Named after the file, with part of the content hash so the same file
    # maps to the same table and different files never collide.
    stem = re.sub(r"[^a-z0-9_]+", "_", Path(file.filename).stem.lower()).strip("_") or "table"
    table_name = f"{stem[:40]}_{content_id[:8]}"
    reused = table_exists(table_name)
    if not reused:
        table_name = create_table_from_csv(str(final_path), table_name)
    return {
        "table_name": table_name,
        "path": str(final_path),
        "reused": reused,
        "columns": describe_table(table_name),
    }


class LoadUrlRequest(BaseModel):
    url: str = Field(..., min_length=1)
    table_name: str | None = None
    # "view" streams from the network and stores nothing locally; "table"
    # copies rows into DuckDB so approximate sampling can skip storage blocks.
    mode: Literal["view", "table"] = "view"
    row_limit: int | None = Field(default=None, gt=0)


def _name_from_url(url: str) -> str:
    stem = Path(urlparse(url).path).name.split(".", 1)[0]
    return re.sub(r"[^a-zA-Z0-9_]", "_", stem) or f"remote_{uuid.uuid4().hex[:8]}"


@router.get("/tables")
def list_tables():
    return list_local_tables()


@router.get("/datasets/public")
def list_public_datasets():
    return PUBLIC_DATASETS


@router.post("/load-url")
def load_url(request: LoadUrlRequest):
    url = request.url.strip()
    # Only remote files: without this check a "URL" like /etc/passwd would be
    # read from the server's own disk.
    if urlparse(url).scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    try:
        table_name = create_table_from_url(
            url,
            request.table_name or _name_from_url(url),
            materialize=request.mode == "table",
            row_limit=request.row_limit,
        )
        columns = describe_table(table_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not load {url}: {exc}") from exc

    return {"table_name": table_name, "mode": request.mode, "columns": columns}
