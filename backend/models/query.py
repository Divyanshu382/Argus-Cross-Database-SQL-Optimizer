from pydantic import BaseModel, Field


class ExecuteRequest(BaseModel):
    query: str = Field(..., min_length=1)
    mode: str = Field(default="exact")
    source: str = Field(default="duckdb")
    request_id: str | None = None
    accuracy_target: float | None = Field(default=None, ge=50.0, le=99.9)
    # Sampler profile for estimates: fast, balanced or accurate (time budget per run).
    profile: str | None = Field(default=None, pattern="^(fast|balanced|accurate)$")
    # False forces a fresh run, so timings are real rather than cache lookups.
    use_cache: bool = True


class PlanRequest(BaseModel):
    query: str = Field(..., min_length=1)
    source: str = Field(default="duckdb")


class UploadResponse(BaseModel):
    table_name: str
    path: str
