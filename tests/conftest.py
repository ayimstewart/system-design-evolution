import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

ENV_KEYS = [
    "DATABASE_URL", "REPLICA_URLS", "SHARD_URLS", "SHARD_REPLICA_URLS", "REDIS_URL",
    "QUEUE_ENABLED", "S3_ENDPOINT", "S3_PUBLIC_URL", "MEDIA_CDN_URL",
    "SIMULATED_WORK_MS", "SIMULATED_QUERY_MS", "OTEL_EXPORTER_OTLP_ENDPOINT",
]


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    """Build a TestClient for a given stage's environment variables."""
    import context
    from fastapi.testclient import TestClient

    clients = []

    def _make(**env):
        for key in ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        settings = {"DATABASE_URL": f"sqlite:///{tmp_path}/primary.db",
                    "PROCESSING_DELAY_SECONDS": "0"}
        settings.update({k: v.format(tmp=tmp_path) for k, v in env.items()})
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
        context.reset_context()
        import main
        client = TestClient(main.app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)
    context.reset_context()
