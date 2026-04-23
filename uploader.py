import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

UPLOAD_TIMEOUT = 30


def post_json(url: str, payload: dict, timeout: int = 10) -> Optional[requests.Response]:
    """POST JSON; returns Response or None on network/timeout failure."""
    try:
        return requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("POST %s failed: %s", url, exc)
        return None


def move_to_pending(stem: str, image_save_dir: Path, pending_uploads_dir: Path) -> None:
    for suffix in ("_raw.jpg", "_annotated.jpg", ".json"):
        src = image_save_dir / f"{stem}{suffix}"
        if src.exists():
            shutil.move(str(src), pending_uploads_dir / src.name)


def delete_bundle(stem: str, image_save_dir: Path) -> None:
    for suffix in ("_raw.jpg", "_annotated.jpg", ".json"):
        f = image_save_dir / f"{stem}{suffix}"
        try:
            f.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Cannot delete %s: %s", f, exc)


def _upload_bundle_raw(
    stem: str,
    job_id: str,
    step_index: int,
    upload_url: str,
    device_id: str,
    device_secret: str,
    image_save_dir: Path,
) -> Optional[requests.Response]:
    """Multipart POST for one bundle. Returns Response or None on failure."""
    raw = image_save_dir / f"{stem}_raw.jpg"
    ann = image_save_dir / f"{stem}_annotated.jpg"
    jsn = image_save_dir / f"{stem}.json"
    try:
        with open(raw, "rb") as rf, open(ann, "rb") as af, open(jsn, "rb") as jf:
            files = {
                "raw_image": (raw.name, rf, "image/jpeg"),
                "annotated_image": (ann.name, af, "image/jpeg"),
                "detections": (jsn.name, jf, "application/json"),
            }
            data = {
                "device_id": device_id,
                "device_secret": device_secret,
                "job_id": job_id,
                "step_index": str(step_index),
            }
            return requests.post(upload_url, files=files, data=data, timeout=UPLOAD_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Upload network error for stem %s: %s", stem, exc)
        return None
    except OSError as exc:
        logger.error("Cannot open bundle files for stem %s: %s", stem, exc)
        return None


def upload_bundle_with_retry(
    stem: str,
    job_id: str,
    step_index: int,
    upload_url: str,
    device_id: str,
    device_secret: str,
    image_save_dir: Path,
    pending_uploads_dir: Path,
    led_send: Callable[[str], None],
    stop_flag: Optional[threading.Event] = None,
) -> str:
    """
    Upload bundle; retry once on non-401 failure with 5-second interruptible wait.

    Returns one of: 'success', 'auth_fail', 'moved', 'stopped'
        'success'   — uploaded; local files deleted
        'auth_fail' — 401; files moved to pending
        'moved'     — both attempts failed; files moved to pending
        'stopped'   — stop_flag set during retry wait; files moved to pending
    """
    resp = _upload_bundle_raw(stem, job_id, step_index, upload_url, device_id, device_secret, image_save_dir)

    if resp is not None and resp.status_code == 200:
        delete_bundle(stem, image_save_dir)
        return "success"

    if resp is not None and resp.status_code == 401:
        logger.error("Auth fail (401) uploading %s", stem)
        led_send("LED:AUTH_FAIL\n")
        move_to_pending(stem, image_save_dir, pending_uploads_dir)
        return "auth_fail"

    # Non-401 failure: wait 5 s, checking stop_flag every 2 s
    logger.warning("Upload failed for stem %s; retrying after 5 s", stem)
    wait_end = time.monotonic() + 5.0
    while time.monotonic() < wait_end:
        if stop_flag and stop_flag.is_set():
            move_to_pending(stem, image_save_dir, pending_uploads_dir)
            return "stopped"
        remaining = wait_end - time.monotonic()
        time.sleep(min(2.0, max(0.0, remaining)))

    resp2 = _upload_bundle_raw(stem, job_id, step_index, upload_url, device_id, device_secret, image_save_dir)
    if resp2 is not None and resp2.status_code == 200:
        delete_bundle(stem, image_save_dir)
        return "success"

    move_to_pending(stem, image_save_dir, pending_uploads_dir)
    return "moved"
