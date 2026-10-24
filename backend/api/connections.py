from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.db import connections as registry
from backend.db import mongo
from backend.db.duckdb import describe_relation

router = APIRouter()


class ConnectRequest(BaseModel):
    name: str = Field(..., min_length=1)
    kind: Literal["postgres", "mysql", "mongodb"]
    # Either a URI (postgres://, mysql://, mongodb://, mongodb+srv://) or the
    # individual fields below.
    uri: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, gt=0, lt=65536)
    database: str | None = None
    user: str | None = None
    password: str | None = None
    # Postgres: limit to one schema. Also the schema read in passthrough mode.
    schema_name: str | None = None


def _params(req: ConnectRequest) -> registry.ConnectionParams:
    extra = {"schema_name": req.schema_name or None}
    if req.uri:
        params = registry.ConnectionParams.from_uri(req.kind, req.uri.strip(), **extra)
        # Fields typed alongside a URI fill in what the URI leaves out.
        params.database = params.database or (req.database or "")
        return params
    if not req.host:
        raise HTTPException(status_code=400, detail="Give either a connection URI or a host.")
    return registry.ConnectionParams(
        kind=req.kind,
        host=req.host.strip(),
        port=req.port,
        database=(req.database or "").strip(),
        user=(req.user or "").strip(),
        password=req.password or "",
        **extra,
    )


@router.get("/connections")
def list_connections():
    return [connection.public() for connection in registry.list_connections()]


@router.get("/connections/public")
def list_public_databases():
    # The published passwords stay server-side; clients connect by name.
    return [
        {key: value for key, value in db.items() if key != "password"}
        for db in registry.PUBLIC_DATABASES
    ]


async def _connect(name: str, params: registry.ConnectionParams):
    try:
        # Connecting can take several seconds (catalog reads, Mongo imports).
        connection = await run_in_threadpool(registry.connect, name, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return connection.public()


@router.post("/connections")
async def connect(req: ConnectRequest):
    return await _connect(req.name.strip(), _params(req))


@router.post("/connections/public/{name}")
async def connect_public(name: str):
    preset = next((db for db in registry.PUBLIC_DATABASES if db["name"] == name), None)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"No public database named '{name}'")
    fields = {key: value for key, value in preset.items() if key not in ("name", "label")}
    return await _connect(name, registry.ConnectionParams(**fields))


@router.delete("/connections/{name}")
def disconnect(name: str):
    try:
        registry.disconnect(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No connection named '{name}'") from exc
    return {"disconnected": name}


@router.get("/columns")
def columns(table: str):
    entry = mongo.lookup(table)
    if entry is not None:
        # The stub table holds whatever the last query fetched; the
        # registry has the full inferred schema.
        return [{"name": name, "type": entry.types.get(name, "VARCHAR")} for name in entry.paths]
    try:
        return describe_relation(table)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not describe {table}: {exc}") from exc
