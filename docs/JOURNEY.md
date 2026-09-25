# The journey, stage by stage

Each stage follows the same shape: **symptom → fix → see it → cost**. Run `make load` before and after every step and compare p95 latency and error rate. That's the whole point: you should *feel* each problem before you fix it.

---

## Stage 0 — One server

`make up STAGE=0`

One FastAPI container, one Postgres. This handles more traffic than most side projects will ever see. Everything is easy: one place to deploy, one place to look, and every query can join anything.

**See it:** `make load`. With the simulated CPU cost, one Python process tops out at roughly 50 reads/second and p95 latency climbs as virtual users stack up.

---

## Stage 1 — Load balancer

**Symptom:** CPU is pinned on the one box. Latency climbs with traffic.
**Fix:** run three copies of the app behind nginx.

**See it:** run `make watch` in one terminal. `x-served-by` / `host` rotates between three containers. `make load` again: throughput roughly triples.

**Cost:** any state kept on a server (sessions, uploaded files on local disk) now breaks, because the next request may land somewhere else. That's why this app keeps *nothing* locally. And note what didn't get faster: all three servers still share one database.

---

## Stage 2 — Cache

**Symptom:** three servers now send three times the queries to one Postgres. The DB's connection pool is the new bottleneck.
**Fix:** Redis in front of the hot read (`GET /users/{id}/posts`), 30-second TTL, deleted on every write.

**See it:** `curl -i` the same user twice: `x-cache: MISS`, then `HIT`. In Grafana later, the cache hit ratio panel.

**Cost:** *caches go stale.* We delete the key on write, but a write that lands between another request's DB read and its cache write can put old data back for up to 30 seconds. Also: if Redis dies, [`app/cache.py`](../app/cache.py) treats errors as misses, so the app degrades instead of falling over. Try `make chaos TARGET=redis`.

---

## Stage 3 — Read replicas

**Symptom:** cache misses, cold users and new features still send more reads than one primary can serve.
**Fix:** a Postgres streaming replica. Writes go to the primary; reads go to the replica.

**See it:** `x-db-role: replica` on list requests. Kill it with `make chaos TARGET=db-replica`: reads fail over to the primary (`x-db-role: primary`) and the `db_replica_failovers_total` metric ticks up.

**Cost:** *replicas lag.* A post you just wrote might not be on the replica yet. Worse, combine with stage 2: write → cache invalidated → next read hits a lagging replica → *stale data gets cached for 30s*. That's why `GET /users/{id}/posts/{post_id}` (used to check your own upload) reads from the primary. This is called read-your-own-writes consistency.

---

## Stage 4 — Sharding

**Symptom:** the dataset or write volume outgrows one machine. Replicas don't help with writes; every replica holds everything.
**Fix:** two independent primaries. `sha256(user_id) % 2` picks the shard ([`app/db.py`](../app/db.py)).

**See it:** `x-shard` differs across users. `make stats` shows posts spread across both shards. `GET /posts/recent` now has to query *every* shard and merge in memory (`shards_queried: 2`).

**Cost:** *shards make queries harder.* Anything that isn't "one user's data" becomes scatter-gather. IDs had to become UUIDs (no shared auto-increment). And adding a third shard remaps most users: this is resharding, and it's why teams delay sharding as long as possible. You'll see it here: users that moved to shard 1 "lose" their old posts, which are still on shard 0. Real systems write a migration. Here, `make reset`.

---

## Stage 5 — Object storage

**Symptom:** before this stage, uploaded images are stored *in Postgres*. Upload a few photos and run `make stats`: `image_bytes_in_db` balloons. That weight gets copied to every replica, every backup, every restore.
**Fix:** bytes go to an S3-compatible bucket (MinIO locally; S3 or R2 in production). The database stores only the key.

**See it:** new posts' `image_url` points at `localhost:9000/media/...`. The MinIO console is at `localhost:9001` (minioadmin / minioadmin).

**Cost:** two systems that can disagree: a DB row whose file failed to upload, or files with no row (orphans). Images uploaded before this stage still point at the database. Moving them is a backfill job (a good exercise).

---

## Stage 6 — CDN

**Symptom:** a user in Sydney fetching images from a bucket in Virginia pays the round trip on every image.
**Fix:** an edge cache in front of the bucket. Real CDNs run edges in hundreds of cities; here it's one nginx on `localhost:8080`.

**See it:** `curl -I` an image URL twice: `X-Cache-Status: MISS`, then `HIT`. Stop MinIO and cached images still load.

**Cost:** once an edge has a file, getting it *out* is slow and messy. The fix is to never change a file at the same URL: every upload gets a new key, and objects are served `immutable`.

---

## Stage 7 — Queue + workers

**Symptom:** uploading a photo takes 2+ seconds because the request waits for thumbnail processing. Under load, those slow requests tie up app servers.
**Fix:** save the original, enqueue a job in Redis (RQ), return `202 Accepted` immediately. Workers make the thumbnail.

**See it:** upload an image; the response comes back instantly with `status: processing`. Poll `GET /users/{id}/posts/{post_id}` until it's `ready`.

**Cost:** *queues deliver twice.* A worker can finish the job and crash before acknowledging it; the job runs again. [`app/jobs.py`](../app/jobs.py) checks whether the work is already done before doing it (idempotency). Also: users now see a "processing" state, and failures happen after you've already said "OK".

---

## Stage 8 — Redundancy

**Symptom:** a container crashes and requests fail until someone notices.
**Fix:**
- restart policies on every service
- the load balancer ejects a failing app replica and retries the request on another (for GET only; retrying a POST could create a duplicate post)
- two queue workers instead of one
- replica failover (already in the code since stage 3)

**See it:** `make load` in one terminal, `make chaos` in another, a few times. Compare the error rate to doing the same thing at stage 1.

**Cost:** more machines and more money, plus failover paths that only work if you test them. What this laptop can't show is a second *region*: in production you'd run the whole stack twice and shift DNS when one disappears. The primary database is still a single point of failure here; managed Postgres with automatic failover is the usual answer.

---

## Stage 9 — Observability

**Symptom:** something is slow. Is it the cache? A shard? One bad replica? You're guessing.
**Fix:** collect what the app has been emitting all along.

| Signal | Where | Answers |
|---|---|---|
| Metrics | Grafana `localhost:3000` | Is it broken? How badly? Since when? |
| Traces | Jaeger `localhost:16686` | Where did this one request spend its time? |
| Logs | `make logs` (JSON, with `request_id`) | What exactly happened? |

**See it:** run `make load`, open Grafana. Then `make chaos TARGET=db-replica` and watch the failover and DB-role panels change. In Jaeger, pick a slow request and see the SQL and Redis spans inside it.

**Cost:** telemetry has a storage bill, and alerts nobody trusts are worse than none. Start with a few signals tied to what users feel: error rate and latency.

---

## The mistake to avoid

Adding Kafka, Kubernetes, microservices and ten databases *before* you have the problems they solve. Every component above earned its place by fixing a failure you could measure. If you can't name the failure, you don't need the component yet.
