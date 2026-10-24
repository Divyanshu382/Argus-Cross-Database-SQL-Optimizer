import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.advise import router as advise_router
from backend.api.connections import router as connections_router
from backend.api.execute import router as execute_router, query_history
from backend.api.optimize import router as optimize_router
from backend.api.plan import router as plan_router
from backend.api.samples import router as samples_router
from backend.api.suggest import router as suggest_router
from backend.api.upload import router as upload_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(title="AetherQuery Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(execute_router, prefix="/api", tags=["execute"])
app.include_router(plan_router, prefix="/api", tags=["plan"])
app.include_router(upload_router, prefix="/api", tags=["upload"])
app.include_router(optimize_router, prefix="/api", tags=["optimize"])
app.include_router(connections_router, prefix="/api", tags=["connections"])
app.include_router(samples_router, prefix="/api", tags=["samples"])
app.include_router(advise_router, prefix="/api", tags=["cost model"])
app.include_router(suggest_router, prefix="/api", tags=["suggestions"])


@app.get("/")
def root():
    return {"msg": "AetherQuery backend is running"}


@app.get("/history")
def get_history() -> list[dict[str, Any]]:
    """Get recent query history (latest first)."""
    return list(query_history)[::-1]
