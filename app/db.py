"""Database access: shards, primaries, replicas.

Stage 0 has one shard with no replicas, so every call goes to the one database.
Adding replicas (stage 3) or shards (stage 4) changes *where* queries go, not
what the calling code looks like. That's the Router's whole job.
"""
import hashlib
import logging
import random
import time
from contextlib import contextmanager

from sqlalchemy import (
    Column, DateTime, Index, LargeBinary, MetaData, String, Table, Text,
    create_engine, delete, func, insert, select, text, update,
)
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

import observability as obs

log = logging.getLogger("db")
metadata = MetaData()

posts = Table(
    "posts", metadata,
    # UUIDs, not auto-increment: two shards can't share one counter.
    Column("id", String(36), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("body", Text, nullable=False),
    Column("image_key", String(255)),
    Column("thumb_key", String(255)),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_posts_user_created", posts.c.user_id, posts.c.created_at)

# Before stage 5, images live *inside* the database. Watch /debug/stats grow.
blobs = Table(
    "blobs", metadata,
    Column("key", String(255), primary_key=True),
    Column("content_type", String(100)),
    Column("data", LargeBinary, nullable=False),
)


def _engine(url):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # A small pool on purpose: connection contention is a real bottleneck.
    return create_engine(
        url, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_timeout=10,
        connect_args={"connect_timeout": 2},
    )


class Shard:
    def __init__(self, index, primary_url, replica_urls):
        self.index = index
        self.primary = _engine(primary_url)
        self.replicas = [_engine(u) for u in replica_urls]


class Router:
    def __init__(self, shard_urls, replica_urls, simulated_query_ms=0):
        self.shards = [
            Shard(i, url, replica_urls[i] if i < len(replica_urls) else [])
            for i, url in enumerate(shard_urls)
        ]
        self.simulated_query_ms = simulated_query_ms

    def shard_for(self, user_id: str) -> Shard:
        # A stable hash (not Python's hash(), which changes per process).
        # Note: changing the shard count remaps most users. That's resharding,
        # and it's why teams put this decision off as long as they can.
        digest = hashlib.sha256(user_id.encode()).digest()
        return self.shards[int.from_bytes(digest[:8], "big") % len(self.shards)]

    def create_schema(self, attempts=10):
        for shard in self.shards:
            for attempt in range(attempts):
                try:
                    metadata.create_all(shard.primary)
                    break
                except SQLAlchemyError:
                    # Several app replicas race to create tables at boot.
                    if attempt == attempts - 1:
                        raise
                    log.warning("schema create failed, retrying", extra={"shard": shard.index})
                    time.sleep(1 + random.random())

    def _simulate(self):
        if self.simulated_query_ms:
            time.sleep(self.simulated_query_ms / 1000)  # holds the connection, like a real slow query

    @contextmanager
    def write(self, shard: Shard):
        with shard.primary.begin() as conn:
            obs.DB_QUERIES.labels(shard=str(shard.index), role="primary").inc()
            self._simulate()
            yield conn

    @contextmanager
    def read(self, shard: Shard):
        conn, role = self._reader(shard)
        try:
            obs.DB_QUERIES.labels(shard=str(shard.index), role=role).inc()
            self._simulate()
            yield conn, role
        finally:
            conn.close()

    def _reader(self, shard: Shard):
        # Try replicas in random order; if they're all down, read from the primary.
        for engine in random.sample(shard.replicas, len(shard.replicas)):
            try:
                return engine.connect(), "replica"
            except DBAPIError:
                obs.REPLICA_FAILOVERS.labels(shard=str(shard.index)).inc()
                log.warning("replica unavailable, failing over", extra={"shard": shard.index})
        return shard.primary.connect(), "primary"


# ---- queries -----------------------------------------------------------------

def insert_post(router: Router, post: dict) -> int:
    shard = router.shard_for(post["user_id"])
    with router.write(shard) as conn:
        conn.execute(insert(posts).values(**post))
    return shard.index


def update_post(router: Router, user_id: str, post_id: str, **values):
    with router.write(router.shard_for(user_id)) as conn:
        conn.execute(
            update(posts).where(posts.c.id == post_id, posts.c.user_id == user_id).values(**values)
        )


def get_post(router: Router, user_id: str, post_id: str):
    """Read from the primary: right after a write, a replica may not have it yet."""
    with router.write(router.shard_for(user_id)) as conn:
        row = conn.execute(
            select(posts).where(posts.c.id == post_id, posts.c.user_id == user_id)
        ).mappings().first()
    return dict(row) if row else None


def list_posts(router: Router, user_id: str, limit=20):
    shard = router.shard_for(user_id)
    with router.read(shard) as (conn, role):
        rows = conn.execute(
            select(posts).where(posts.c.user_id == user_id)
            .order_by(posts.c.created_at.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows], role, shard.index


def recent_posts(router: Router, limit=20):
    """Scatter-gather across every shard, then merge.

    With one database this is a single ORDER BY. With N shards it's N queries
    plus an in-memory merge. This is the "shards make queries harder" tax.
    """
    merged = []
    for shard in router.shards:
        with router.read(shard) as (conn, _):
            merged.extend(
                dict(r) for r in conn.execute(
                    select(posts).order_by(posts.c.created_at.desc()).limit(limit)
                ).mappings()
            )
    merged.sort(key=lambda p: p["created_at"], reverse=True)
    return merged[:limit]


def _shard_for_key(router: Router, key: str) -> Shard:
    return router.shard_for(key.split("/", 1)[0])  # keys look like "<user_id>/<post_id>/..."


def put_blob(router: Router, key: str, data: bytes, content_type: str):
    with router.write(_shard_for_key(router, key)) as conn:
        conn.execute(delete(blobs).where(blobs.c.key == key))
        conn.execute(insert(blobs).values(key=key, data=data, content_type=content_type))


def get_blob(router: Router, key: str, primary_only=False):
    shard = _shard_for_key(router, key)
    query = select(blobs.c.data, blobs.c.content_type).where(blobs.c.key == key)
    if not primary_only:
        with router.read(shard) as (conn, _):
            row = conn.execute(query).first()
        if row:
            return bytes(row.data), row.content_type
    with router.write(shard) as conn:  # replica lag fallback
        row = conn.execute(query).first()
    return (bytes(row.data), row.content_type) if row else None


def stats(router: Router):
    out = []
    for shard in router.shards:
        with router.write(shard) as conn:
            n_posts = conn.execute(select(func.count()).select_from(posts)).scalar_one()
            n_blobs, blob_bytes = conn.execute(
                select(func.count(), func.coalesce(func.sum(func.length(blobs.c.data)), 0))
                .select_from(blobs)
            ).one()
            db_bytes = None
            if conn.dialect.name == "postgresql":
                db_bytes = conn.execute(text("select pg_database_size(current_database())")).scalar_one()
        out.append({
            "shard": shard.index,
            "replicas": len(shard.replicas),
            "posts": n_posts,
            "images_in_db": n_blobs,
            "image_bytes_in_db": int(blob_bytes),
            "database_bytes": db_bytes,
        })
    return out
