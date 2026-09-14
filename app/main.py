"""Cyber inventory service.

A small FastAPI service that tracks assets. The interesting parts for the
boot camp are not the CRUD endpoints -- they are:

  * /version   build provenance, so you can trace a running container to a commit
  * /healthz   liveness, for Cloud Run
  * /readyz    readiness, distinct from liveness (see the note on that handler)
  * structured JSON logging with a request id, so logs are greppable in
    Cloud Logging rather than being a wall of text
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

from app.models import Asset, AssetCreate, AssetUpdate, HealthStatus
from app.store import AssetStore, DuplicateAssetError, get_store
from app.version import get_build_info

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# Cloud Logging parses stdout as structured data when it is JSON, and treats
# the "severity" key specially. Emitting plain text here would cost you log
# levels and field-based filtering in production -- exactly the tooling you
# want during the failure-diagnosis exercise.


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        # Anything attached via logger.info(..., extra={...}) rides along.
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    return logging.getLogger("cyber-inventory")


log = _configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    build = get_build_info()
    # Logging provenance at startup means the very first line in Cloud
    # Logging for any revision tells you what is running. This is the
    # cheapest possible traceability win.
    log.info(
        "service starting",
        extra={"extra_fields": {**build.as_dict(), "event": "startup"}},
    )
    if not build.is_traceable:
        log.warning(
            "build provenance missing -- container was not built by CI",
            extra={"extra_fields": {"event": "provenance_missing"}},
        )
    yield
    log.info("service stopping", extra={"extra_fields": {"event": "shutdown"}})


app = FastAPI(
    title="Cyber Inventory Service",
    description="Asset inventory with full commit-to-production traceability.",
    version=get_build_info().version,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id and log one structured line per request.

    Cloud Run forwards a trace header; reusing it when present means your
    application logs correlate with the platform's request logs instead of
    living in a parallel universe.
    """
    request_id = request.headers.get("X-Cloud-Trace-Context", "").split("/")[0]
    request_id = request_id or str(uuid.uuid4())
    request.state.request_id = request_id

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception(
            "unhandled exception",
            extra={"extra_fields": {"request_id": request_id,
                                    "path": request.url.path}},
        )
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    response.headers["X-Request-ID"] = request_id
    # Surfacing the commit on every response means anyone -- including a
    # teammate with only curl -- can identify the running build.
    response.headers["X-Build-Commit"] = get_build_info().commit_short

    log.info(
        "request handled",
        extra={"extra_fields": {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        }},
    )
    return response


# --------------------------------------------------------------------------
# Operational endpoints
# --------------------------------------------------------------------------


# Cloud Run's frontend RESERVES paths ending in "z" and 404s them before
# they reach the container. /healthz is the Kubernetes convention, so it
# stays registered as an alias -- it works locally, in Docker and on GKE.
@app.get("/health", response_model=HealthStatus, tags=["ops"])
@app.get("/healthz", response_model=HealthStatus, tags=["ops"],
         include_in_schema=False)
def health() -> HealthStatus:
    """Liveness: is the process up and able to respond at all?

    Deliberately checks nothing external. A liveness probe that depends on a
    database will restart a perfectly healthy container during a database
    blip, turning a partial outage into a total one.
    """
    return HealthStatus(status="ok", checks={"process": "ok"})


@app.get("/ready", response_model=HealthStatus, tags=["ops"])
@app.get("/readyz", response_model=HealthStatus, tags=["ops"],
         include_in_schema=False)
def ready(store: AssetStore = Depends(get_store)) -> HealthStatus:
    """Readiness: should this instance receive traffic right now?

    This is where dependency checks belong. Today the only dependency is the
    in-memory store; when a real database arrives, its check goes here and
    nowhere near /healthz.
    """
    checks = {"process": "ok", "store": "ok"}
    try:
        store.count()
    except Exception:  # pragma: no cover - defensive
        checks["store"] = "error"
        return HealthStatus(status="degraded", checks=checks)
    return HealthStatus(status="ok", checks=checks)


@app.get("/version", tags=["ops"])
def version() -> dict[str, str]:
    """Which commit is actually running.

    The single most useful endpoint in this service during an incident.
    """
    return get_build_info().as_dict()


# --------------------------------------------------------------------------
# Asset endpoints
# --------------------------------------------------------------------------


@app.post(
    "/api/v1/assets",
    response_model=Asset,
    status_code=status.HTTP_201_CREATED,
    tags=["assets"],
)
def create_asset(
    payload: AssetCreate, store: AssetStore = Depends(get_store)
) -> Asset:
    try:
        asset = store.create(payload)
    except DuplicateAssetError as exc:
        # 409, not 400: the request is well-formed, it conflicts with state.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    log.info(
        "asset registered",
        extra={"extra_fields": {"asset_id": asset.id,
                                "hostname": asset.hostname,
                                "event": "asset_created"}},
    )
    return asset


@app.get("/api/v1/assets", response_model=list[Asset], tags=["assets"])
def list_assets(
    criticality: str | None = Query(default=None),
    asset_type: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    store: AssetStore = Depends(get_store),
) -> list[Asset]:
    return store.list(criticality=criticality, asset_type=asset_type, tag=tag)


@app.get("/api/v1/assets/{asset_id}", response_model=Asset, tags=["assets"])
def get_asset(asset_id: str, store: AssetStore = Depends(get_store)) -> Asset:
    asset = store.get(asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found")
    return asset


@app.patch("/api/v1/assets/{asset_id}", response_model=Asset, tags=["assets"])
def update_asset(
    asset_id: str,
    payload: AssetUpdate,
    store: AssetStore = Depends(get_store),
) -> Asset:
    asset = store.update(asset_id, payload)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found")
    return asset


@app.delete(
    "/api/v1/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # response_class is required here: FastAPI refuses to build a response
    # model for a 204, since HTTP forbids a body on that status. Annotating
    # the handler `-> None` is not enough -- it still tries.
    response_class=Response,
    tags=["assets"],
)
def delete_asset(
    asset_id: str, store: AssetStore = Depends(get_store)
) -> Response:
    if not store.delete(asset_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/v1/stats", tags=["assets"])
def stats(store: AssetStore = Depends(get_store)) -> dict[str, object]:
    assets = store.list()
    by_criticality: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for asset in assets:
        by_criticality[asset.criticality] = (
            by_criticality.get(asset.criticality, 0) + 1
        )
        by_type[asset.asset_type] = by_type.get(asset.asset_type, 0) + 1
    return {
        "total": len(assets),
        "by_criticality": by_criticality,
        "by_type": by_type,
    }


@app.exception_handler(DuplicateAssetError)
async def duplicate_handler(request: Request, exc: DuplicateAssetError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})
