import io

import fakeredis
import pytest
from PIL import Image


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 80, 40)).save(buf, "PNG")
    return buf.getvalue()


def test_stage0_single_server(make_client):
    client = make_client()
    assert client.post("/users/ada/posts", data={"body": "hello"}).status_code == 201
    res = client.get("/users/ada/posts")
    assert res.status_code == 200
    assert [p["body"] for p in res.json()["posts"]] == ["hello"]
    assert res.headers["x-cache"] == "BYPASS"
    assert res.headers["x-db-role"] == "primary"


def test_rejects_bad_user_id(make_client):
    client = make_client()
    assert client.get("/users/bad%20id/posts").status_code == 422


def test_cache_hit_and_invalidation(make_client, monkeypatch):
    import redis
    server = fakeredis.FakeServer()
    monkeypatch.setattr(redis.Redis, "from_url",
                        classmethod(lambda cls, *a, **k: fakeredis.FakeRedis(server=server, decode_responses=True)))
    client = make_client(REDIS_URL="redis://fake:6379/0")
    client.post("/users/ada/posts", data={"body": "one"})
    assert client.get("/users/ada/posts").headers["x-cache"] == "MISS"
    assert client.get("/users/ada/posts").headers["x-cache"] == "HIT"
    client.post("/users/ada/posts", data={"body": "two"})  # write invalidates
    res = client.get("/users/ada/posts")
    assert res.headers["x-cache"] == "MISS"
    assert [p["body"] for p in res.json()["posts"]] == ["two", "one"]


def test_cache_outage_is_just_a_miss(make_client):
    client = make_client(REDIS_URL="redis://127.0.0.1:1/0")  # nothing listening
    client.post("/users/ada/posts", data={"body": "still works"})
    res = client.get("/users/ada/posts")
    assert res.status_code == 200
    assert res.json()["posts"][0]["body"] == "still works"


def test_reads_go_to_replica(make_client):
    # Point the "replica" at the same file: perfect, zero-lag replication.
    client = make_client(REPLICA_URLS="sqlite:///{tmp}/primary.db")
    client.post("/users/ada/posts", data={"body": "hi"})
    res = client.get("/users/ada/posts")
    assert res.headers["x-db-role"] == "replica"
    assert res.json()["posts"][0]["body"] == "hi"


def test_dead_replica_falls_back_to_primary(make_client):
    client = make_client(REPLICA_URLS="postgresql+psycopg://x:x@127.0.0.1:1/x")
    client.post("/users/ada/posts", data={"body": "hi"})
    res = client.get("/users/ada/posts")
    assert res.status_code == 200
    assert res.headers["x-db-role"] == "primary"


def test_sharding_spreads_users_and_merges_feed(make_client):
    client = make_client(SHARD_URLS="sqlite:///{tmp}/s0.db,sqlite:///{tmp}/s1.db")
    homes = {}
    for i in range(40):
        res = client.post(f"/users/user{i}/posts", data={"body": f"post {i}"})
        homes[f"user{i}"] = res.headers["x-shard"]

    shards = client.get("/debug/stats").json()["shards"]
    assert len(shards) == 2
    assert all(s["posts"] > 0 for s in shards)
    assert sum(s["posts"] for s in shards) == 40

    for user, shard in list(homes.items())[:5]:  # routing is stable
        res = client.get(f"/users/{user}/posts")
        assert res.headers["x-shard"] == shard
        assert len(res.json()["posts"]) == 1

    feed = client.get("/posts/recent").json()
    assert feed["shards_queried"] == 2
    assert [p["body"] for p in feed["posts"]] == [f"post {i}" for i in range(39, 19, -1)]


def test_inline_image_upload_makes_thumbnail(make_client):
    client = make_client()
    res = client.post("/users/ada/posts", data={"body": "pic"},
                      files={"image": ("cat.png", png_bytes(), "image/png")})
    assert res.status_code == 201
    post = res.json()
    assert post["status"] == "ready"
    thumb = client.get(post["thumb_url"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(thumb.content)).size) <= 256

    stats = client.get("/debug/stats").json()
    assert stats["image_storage"] == "database"
    assert stats["shards"][0]["images_in_db"] == 2  # original + thumbnail, inside the DB


def test_job_is_idempotent(make_client):
    import jobs
    client = make_client()
    post = client.post("/users/ada/posts", data={"body": "pic"},
                       files={"image": ("cat.png", png_bytes(), "image/png")}).json()
    key = f"ada/{post['id']}/original.png"
    assert jobs.process_image("ada", post["id"], key) == "duplicate"


def test_garbage_image_is_marked_failed(make_client):
    client = make_client()
    res = client.post("/users/ada/posts", data={"body": "oops"},
                      files={"image": ("x.png", b"not an image", "image/png")})
    assert res.json()["status"] == "failed"


def test_metrics_and_index(make_client):
    client = make_client()
    client.get("/users/ada/posts")
    assert "http_requests_total" in client.get("/metrics").text
    assert client.get("/").json()["features"]["shards"] == 1


def test_queue_requires_redis(monkeypatch):
    import config
    monkeypatch.setenv("QUEUE_ENABLED", "true")
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValueError):
        config.load_settings()
