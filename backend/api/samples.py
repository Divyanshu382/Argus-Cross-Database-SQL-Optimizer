from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.db import samples

router = APIRouter()


class BuildSampleRequest(BaseModel):
    table: str = Field(..., min_length=1)
    # 1.0 makes a full local copy (for small tables joined against).
    fraction: float = Field(default=0.01, gt=0, le=1.0)


@router.get("/samples")
def list_samples():
    return [sample.public() for sample in samples.list_samples()]


@router.get("/samples/lookup")
def lookup_sample(table: str):
    sample = samples.lookup(table)
    return sample.public() if sample else None


@router.post("/samples")
async def build_sample(req: BuildSampleRequest):
    try:
        # A full pass over the source table; minutes for tens of millions of rows.
        sample = await run_in_threadpool(samples.build, req.table, req.fraction)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Building the sample failed: {exc}") from exc
    return sample.public()


@router.delete("/samples")
def delete_sample(table: str):
    try:
        samples.delete(table)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No stored sample for {table}") from exc
    return {"deleted": table}
