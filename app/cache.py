"""Redis cache (stage 2).

Two rules this file follows:
  1. A cache failure is a cache miss, never an outage.
  2. Every write deletes the keys it affects. Even so, entries can go stale
     (see the replica-lag note in docs/JOURNEY.md, stage 3).
"""
import json
import logging

import redis

import observability as obs

log = logging.getLogger("cache")


class Cache:
    def __init__(self, url, ttl_seconds):
        self.ttl = ttl_seconds
        self.client = (
            redis.Redis.from_url(url, decode_responses=True, socket_timeout=0.5,
                                 socket_connect_timeout=0.5)
            if url else None
        )

    @property
    def enabled(self):
        return self.client is not None

    def get(self, key):
        if not self.client:
            return None
        try:
            raw = self.client.get(key)
        except redis.RedisError:
            obs.CACHE.labels(result="error").inc()
            log.warning("cache unavailable, treating as miss")
            return None
        obs.CACHE.labels(result="hit" if raw else "miss").inc()
        return json.loads(raw) if raw else None

    def set(self, key, value):
        if not self.client:
            return
        try:
            self.client.set(key, json.dumps(value), ex=self.ttl)
        except redis.RedisError:
            log.warning("cache write failed")

    def delete(self, *keys):
        if not self.client or not keys:
            return
        try:
            self.client.delete(*keys)
        except redis.RedisError:
            log.warning("cache invalidation failed; entries expire in %ss", self.ttl)
