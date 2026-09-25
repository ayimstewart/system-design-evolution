"""Builds the shared objects (router, storage, cache) once per process."""
import threading
from dataclasses import dataclass

from cache import Cache
from config import Settings, load_settings
from db import Router
from storage import DatabaseStore, S3Store


@dataclass
class Context:
    settings: Settings
    router: Router
    storage: object
    cache: Cache


_ctx = None
_lock = threading.Lock()


def get_context() -> Context:
    global _ctx
    if _ctx is None:
        with _lock:
            if _ctx is None:
                s = load_settings()
                router = Router(s.shard_urls, s.shard_replica_urls, s.simulated_query_ms)
                storage = S3Store(s) if s.s3_endpoint else DatabaseStore(router)
                _ctx = Context(s, router, storage, Cache(s.redis_url, s.cache_ttl_seconds))
    return _ctx


def reset_context():
    global _ctx
    _ctx = None
