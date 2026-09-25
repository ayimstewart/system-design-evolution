"""Every component in this project is switched on by an environment variable.

Each stage's compose overlay flips one more switch, so the *same code* runs as
a single box in stage 0 and as a sharded, cached, queued system in stage 9.
"""
import os
from dataclasses import dataclass


def _get(name, default=None):
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _split(value, sep=","):
    return [part.strip() for part in (value or "").split(sep) if part.strip()]


def _bool(name):
    return (_get(name, "false") or "").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    shard_urls: list            # one primary per shard
    shard_replica_urls: list    # list of replica URLs for each shard
    redis_url: str | None
    cache_ttl_seconds: int
    queue_enabled: bool
    s3_endpoint: str | None
    s3_bucket: str
    s3_access_key: str | None
    s3_secret_key: str | None
    s3_public_url: str | None
    media_cdn_url: str | None
    processing_delay_seconds: float
    simulated_work_ms: int
    simulated_query_ms: int
    otel_endpoint: str | None

    def features(self) -> dict:
        return {
            "shards": len(self.shard_urls),
            "read_replicas": sum(len(r) for r in self.shard_replica_urls),
            "cache": bool(self.redis_url),
            "object_storage": bool(self.s3_endpoint),
            "cdn": bool(self.media_cdn_url),
            "queue": self.queue_enabled,
            "tracing": bool(self.otel_endpoint),
        }


def load_settings() -> Settings:
    # SHARD_URLS=url0,url1            (one primary per shard)
    # SHARD_REPLICA_URLS=a,b;c        (';' between shards, ',' between replicas)
    # Without SHARD_URLS we have a single shard: DATABASE_URL + REPLICA_URLS.
    shard_env = _get("SHARD_URLS")
    if shard_env:
        shard_urls = _split(shard_env)
        groups = (os.getenv("SHARD_REPLICA_URLS") or "").split(";")
        replicas = [_split(groups[i]) if i < len(groups) else [] for i in range(len(shard_urls))]
    else:
        shard_urls = [_get("DATABASE_URL", "sqlite:///./local.db")]
        replicas = [_split(os.getenv("REPLICA_URLS"))]

    settings = Settings(
        shard_urls=shard_urls,
        shard_replica_urls=replicas,
        redis_url=_get("REDIS_URL"),
        cache_ttl_seconds=int(_get("CACHE_TTL_SECONDS", "30")),
        queue_enabled=_bool("QUEUE_ENABLED"),
        s3_endpoint=_get("S3_ENDPOINT"),
        s3_bucket=_get("S3_BUCKET", "media"),
        s3_access_key=_get("S3_ACCESS_KEY"),
        s3_secret_key=_get("S3_SECRET_KEY"),
        s3_public_url=_get("S3_PUBLIC_URL"),
        media_cdn_url=_get("MEDIA_CDN_URL"),
        processing_delay_seconds=float(_get("PROCESSING_DELAY_SECONDS", "2")),
        simulated_work_ms=int(_get("SIMULATED_WORK_MS", "0")),
        simulated_query_ms=int(_get("SIMULATED_QUERY_MS", "0")),
        otel_endpoint=_get("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    if settings.queue_enabled and not settings.redis_url:
        raise ValueError("QUEUE_ENABLED needs REDIS_URL (the queue lives in Redis)")
    return settings
