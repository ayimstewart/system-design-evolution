"""Logs, metrics and traces.

The app emits JSON logs and Prometheus metrics from stage 0 onwards (they're
cheap). Stage 9 is where we finally *collect* them, plus turn on tracing.
"""
import json
import logging
import os
import socket
import sys
import time

from prometheus_client import Counter, Histogram

HOSTNAME = socket.gethostname()

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
CACHE = Counter("cache_requests_total", "Cache lookups", ["result"])  # hit | miss | error
DB_QUERIES = Counter("db_queries_total", "Database round trips", ["shard", "role"])
REPLICA_FAILOVERS = Counter(
    "db_replica_failovers_total", "Reads that skipped a dead replica", ["shard"]
)
JOBS = Counter("image_jobs_total", "Image processing jobs", ["mode"])  # inline | queued


class JsonFormatter(logging.Formatter):
    _reserved = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "host": HOSTNAME,
        }
        payload.update({k: v for k, v in vars(record).items() if k not in self._reserved})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def setup_tracing(app) -> bool:
    """Send traces over OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set.

    Must run before any database engine is created so SQLAlchemy gets patched.
    """
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    SQLAlchemyInstrumentor().instrument()
    RedisInstrumentor().instrument()
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    logging.getLogger("tracing").info("tracing enabled")
    return True
