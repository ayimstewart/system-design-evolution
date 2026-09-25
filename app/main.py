"""A tiny photo-posting API that grows from one box to a distributed system.

Response headers tell you what happened:
  x-served-by  which app replica answered            (stage 1: load balancer)
  x-cache      HIT / MISS / BYPASS                   (stage 2: cache)
  x-db-role    primary / replica                     (stage 3: read replicas)
  x-shard      which shard holds this user           (stage 4: sharding)
"""
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, Path, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

import db
import jobs
import observability as obs
from context import get_context
from storage import S3Store

obs.setup_logging()
log = logging.getLogger("app")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
USER_ID = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = get_context()
    ctx.router.create_schema()
    if isinstance(ctx.storage, S3Store):
        ctx.storage.ensure_bucket()
    log.info("started", extra={"features": ctx.settings.features()})
    yield


app = FastAPI(title="system-design-evolution", lifespan=lifespan)
obs.setup_tracing(app)  # before engines exist, so SQLAlchemy gets instrumented


@app.middleware("http")
async def telemetry(request: Request, call_next):
    start = time.perf_counter()
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    route_obj = request.scope.get("route")
    route = getattr(route_obj, "path", "unmatched")
    if route not in ("/metrics", "/health"):
        obs.REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        obs.LATENCY.labels(request.method, route).observe(elapsed)
        log.info("request", extra={
            "request_id": request_id, "method": request.method, "route": route,
            "status": response.status_code, "ms": round(elapsed * 1000, 1),
        })
    response.headers["x-request-id"] = request_id
    response.headers["x-served-by"] = obs.HOSTNAME
    return response


def burn_cpu(ms: int):
    """Simulated per-request CPU work (templating, auth, serialization).

    Laptops are fast; without this, one server never looks overloaded.
    """
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        pass


def serialize(post: dict, storage) -> dict:
    created = post["created_at"]
    return {
        "id": post["id"],
        "user_id": post["user_id"],
        "body": post["body"],
        "status": post["status"],
        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
        "image_url": storage.url(post["image_key"]) if post.get("image_key") else None,
        "thumb_url": storage.url(post["thumb_key"]) if post.get("thumb_key") else None,
    }


_queue = None


def image_queue():
    global _queue
    if _queue is None:
        from redis import Redis
        from rq import Queue
        _queue = Queue("images", connection=Redis.from_url(get_context().settings.redis_url))
    return _queue


# ---- routes --------------------------------------------------------------------

@app.get("/")
def index():
    ctx = get_context()
    return {
        "service": "system-design-evolution",
        "host": obs.HOSTNAME,
        "features": ctx.settings.features(),
        "try": [
            "POST /users/{user_id}/posts  (form: body, optional image)",
            "GET  /users/{user_id}/posts",
            "GET  /posts/recent",
            "GET  /debug/stats",
            "GET  /metrics",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok", "host": obs.HOSTNAME}


@app.get("/ready")
def ready():
    ctx = get_context()
    try:
        for shard in ctx.router.shards:
            with shard.primary.connect() as conn:
                conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"database not ready: {exc.__class__.__name__}")
    return {"status": "ready"}


@app.get("/whoami")
def whoami():
    return {"host": obs.HOSTNAME, "pid": os.getpid()}


@app.post("/users/{user_id}/posts", status_code=201)
def create_post(
    user_id: str = USER_ID,
    body: str = Form(..., max_length=5000),
    image: UploadFile | None = File(None),
):
    ctx = get_context()
    post_id = str(uuid.uuid4())
    image_key = None

    if image is not None and image.filename:
        data = image.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "image too large (10 MB max)")
        ext = os.path.splitext(image.filename)[1].lower()[:8] or ".img"
        image_key = f"{user_id}/{post_id}/original{ext}"
        ctx.storage.put(image_key, data, image.content_type or "application/octet-stream")

    post = {
        "id": post_id, "user_id": user_id, "body": body,
        "image_key": image_key, "thumb_key": None,
        "status": "processing" if image_key else "ready",
        "created_at": datetime.now(timezone.utc),
    }
    shard = db.insert_post(ctx.router, post)
    ctx.cache.delete(f"posts:{user_id}", "posts:recent")

    if image_key:
        if ctx.settings.queue_enabled:
            # Stage 7: hand the slow work to a worker and answer right away.
            from rq import Retry
            image_queue().enqueue(
                jobs.process_image, user_id, post_id, image_key,
                job_timeout=120, retry=Retry(max=3, interval=[5, 15, 30]),
            )
            obs.JOBS.labels(mode="queued").inc()
            return JSONResponse(serialize(post, ctx.storage), status_code=202,
                                headers={"x-shard": str(shard)})
        # Before stage 7: the user waits while we process the image.
        obs.JOBS.labels(mode="inline").inc()
        jobs.process_image(user_id, post_id, image_key)
        post = db.get_post(ctx.router, user_id, post_id)

    return JSONResponse(serialize(post, ctx.storage), status_code=201,
                        headers={"x-shard": str(shard)})


@app.get("/users/{user_id}/posts")
def list_posts(response: Response, user_id: str = USER_ID):
    ctx = get_context()
    burn_cpu(ctx.settings.simulated_work_ms)
    key = f"posts:{user_id}"

    cached = ctx.cache.get(key)
    if cached is not None:
        response.headers["x-cache"] = "HIT"
        response.headers["x-shard"] = str(cached["shard"])
        return cached

    rows, role, shard = db.list_posts(ctx.router, user_id)
    payload = {"user_id": user_id, "shard": shard,
               "posts": [serialize(p, ctx.storage) for p in rows]}
    ctx.cache.set(key, payload)
    response.headers["x-cache"] = "MISS" if ctx.cache.enabled else "BYPASS"
    response.headers["x-db-role"] = role
    response.headers["x-shard"] = str(shard)
    return payload


@app.get("/users/{user_id}/posts/{post_id}")
def get_post(post_id: str, user_id: str = USER_ID):
    """Poll this after a queued upload to see status go processing -> ready."""
    ctx = get_context()
    post = db.get_post(ctx.router, user_id, post_id)
    if not post:
        raise HTTPException(404, "post not found")
    return serialize(post, ctx.storage)


@app.get("/posts/recent")
def recent(response: Response):
    ctx = get_context()
    burn_cpu(ctx.settings.simulated_work_ms)
    cached = ctx.cache.get("posts:recent")
    if cached is not None:
        response.headers["x-cache"] = "HIT"
        return cached
    payload = {
        "shards_queried": len(ctx.router.shards),
        "posts": [serialize(p, ctx.storage) for p in db.recent_posts(ctx.router)],
    }
    ctx.cache.set("posts:recent", payload)
    response.headers["x-cache"] = "MISS" if ctx.cache.enabled else "BYPASS"
    return payload


@app.get("/media/{key:path}")
def media(key: str):
    ctx = get_context()
    if isinstance(ctx.storage, S3Store):
        return RedirectResponse(ctx.storage.url(key), status_code=302)
    found = db.get_blob(ctx.router, key)
    if not found:
        raise HTTPException(404, "not found")
    data, content_type = found
    return Response(data, media_type=content_type or "application/octet-stream",
                    headers={"cache-control": "public, max-age=3600"})


@app.get("/debug/stats")
def debug_stats():
    ctx = get_context()
    return {
        "host": obs.HOSTNAME,
        "features": ctx.settings.features(),
        "image_storage": ctx.storage.kind,
        "shards": db.stats(ctx.router),
    }


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
