"""Where image bytes live.

DatabaseStore (stages 0-4): bytes go in a table. Simple, and it quietly bloats
backups, replication traffic and every shard.
S3Store (stage 5+): bytes go to object storage; the database keeps a key.
"""
import json
import logging
import time

import db

log = logging.getLogger("storage")


class DatabaseStore:
    kind = "database"

    def __init__(self, router):
        self.router = router

    def put(self, key, data, content_type):
        db.put_blob(self.router, key, data, content_type)

    def get(self, key):
        found = db.get_blob(self.router, key, primary_only=True)
        if found is None:
            raise KeyError(key)
        return found

    def url(self, key):
        return f"/media/{key}"  # served by the app itself


class S3Store:
    kind = "object-storage"

    def __init__(self, settings):
        import boto3
        from botocore.config import Config

        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )
        # Stage 6: point public URLs at the CDN instead of the bucket.
        base = settings.media_cdn_url or settings.s3_public_url or f"{settings.s3_endpoint}/{self.bucket}"
        self.public_url = base.rstrip("/")

    def ensure_bucket(self, attempts=15):
        from botocore.exceptions import BotoCoreError, ClientError

        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow", "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{self.bucket}/*"],
            }],
        }
        for attempt in range(attempts):
            try:
                try:
                    self.client.head_bucket(Bucket=self.bucket)
                except ClientError:
                    try:
                        self.client.create_bucket(Bucket=self.bucket)
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                            raise
                self.client.put_bucket_policy(Bucket=self.bucket, Policy=json.dumps(policy))
                return
            except (BotoCoreError, ClientError):
                if attempt == attempts - 1:
                    raise
                log.warning("object storage not ready, retrying")
                time.sleep(2)

    def put(self, key, data, content_type):
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
            CacheControl="public, max-age=604800, immutable",  # keys never change, so cache forever
        )

    def get(self, key):
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read(), obj.get("ContentType")

    def url(self, key):
        return f"{self.public_url}/{key}"
