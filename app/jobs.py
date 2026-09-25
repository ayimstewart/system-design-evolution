"""Image processing: inline before stage 7, on a queue from stage 7 onwards."""
import io
import logging
import time

from PIL import Image, UnidentifiedImageError

import db
from context import get_context

log = logging.getLogger("jobs")
THUMB_SIZE = (256, 256)


def process_image(user_id: str, post_id: str, key: str) -> str:
    """Make a thumbnail for a post.

    Queues deliver *at least once*, so this must be safe to run twice.
    We check the post's state first and skip work that's already done.
    """
    ctx = get_context()
    post = db.get_post(ctx.router, user_id, post_id)
    if post is None:
        log.warning("post not found", extra={"post_id": post_id})
        return "missing"
    if post["status"] == "ready" and post["thumb_key"]:
        log.info("duplicate delivery, skipping", extra={"post_id": post_id})
        return "duplicate"

    # Stand-in for the slow part of real media work: transcoding, virus scans, ML tagging.
    time.sleep(ctx.settings.processing_delay_seconds)

    try:
        data, _ = ctx.storage.get(key)
        img = Image.open(io.BytesIO(data))
        img.thumbnail(THUMB_SIZE)
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=85)
        thumb_key = f"{user_id}/{post_id}/thumb.jpg"
        ctx.storage.put(thumb_key, out.getvalue(), "image/jpeg")
        db.update_post(ctx.router, user_id, post_id, thumb_key=thumb_key, status="ready")
        result = "processed"
    except (UnidentifiedImageError, OSError, KeyError):
        log.exception("image processing failed", extra={"post_id": post_id})
        db.update_post(ctx.router, user_id, post_id, status="failed")
        result = "failed"

    ctx.cache.delete(f"posts:{user_id}", "posts:recent")
    log.info("image job finished", extra={"post_id": post_id, "result": result})
    return result
