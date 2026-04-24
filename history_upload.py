import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import settings
import uploader

logger = logging.getLogger(__name__)


def run_history_upload(
    *,
    stop_flag: threading.Event,
    led_send: Callable[[str], None],
) -> None:
    """SR-49–SR-52: History Data Upload handler (runs in handler thread)."""
    pending_dir = Path(settings.get("pending_uploads_dir"))
    upload_url = settings.url("upload_url")
    device_id = settings.get("device_id")
    device_secret = settings.get("device_secret")
    history_url = settings.url("history_upload_completed_url")
    state_url = settings.url("device_state_url")

    # Discover bundles by .json files; stem is the shared filename base
    json_files = sorted(pending_dir.glob("*.json"))
    stems = []
    for jf in json_files:
        stem = jf.stem
        # Each bundle has _raw.jpg, _annotated.jpg, .json; filter by presence of stem (not suffixed)
        # stems from upload: "{ts}_{step_index}.json" — no trailing suffix beyond .json
        stems.append(stem)

    # SR-52: no bundles — still post entry/exit state
    succeeded = 0
    failed = 0

    for stem in stems:
        if stop_flag.is_set():
            break

        jsn_path = pending_dir / f"{stem}.json"
        try:
            meta = json.loads(jsn_path.read_text())
            job_id = meta["job_id"]
            step_index = meta["step_index"]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            logger.error("Cannot read bundle meta %s: %s", jsn_path, exc)
            failed += 1
            continue

        # Upload from pending_dir (treat it as image_save_dir for this call)
        resp = uploader._upload_bundle_raw(
            stem=stem,
            job_id=job_id,
            step_index=step_index,
            upload_url=upload_url,
            device_id=device_id,
            device_secret=device_secret,
            image_save_dir=pending_dir,
        )

        if resp is not None and resp.status_code == 200:
            uploader.delete_bundle(stem, pending_dir)
            succeeded += 1
            if stop_flag.is_set():
                break
            continue

        if resp is not None and resp.status_code == 401:
            logger.error("History upload auth fail (401) for stem %s", stem)
            led_send("LED:AUTH_FAIL\n")
            failed += 1
            if stop_flag.is_set():
                break
            continue

        # Non-401 failure: retry after 5 s, checking stop_flag every 2 s
        logger.warning("History upload failed for stem %s; retrying after 5 s", stem)
        wait_end = time.monotonic() + 5.0
        while time.monotonic() < wait_end:
            if stop_flag.is_set():
                failed += 1
                break
            remaining = wait_end - time.monotonic()
            time.sleep(min(2.0, max(0.0, remaining)))
        else:
            resp2 = uploader._upload_bundle_raw(
                stem=stem,
                job_id=job_id,
                step_index=step_index,
                upload_url=upload_url,
                device_id=device_id,
                device_secret=device_secret,
                image_save_dir=pending_dir,
            )
            if resp2 is not None and resp2.status_code == 200:
                uploader.delete_bundle(stem, pending_dir)
                succeeded += 1
            else:
                failed += 1

        if stop_flag.is_set():
            break

    # SR-51: post completion
    comp_resp = uploader.post_json(
        history_url,
        {"device_id": device_id, "device_secret": device_secret, "succeeded": succeeded, "failed": failed},
        timeout=10,
    )
    if comp_resp is None:
        logger.error("History upload: completion POST failed (network)")
    elif comp_resp.status_code == 401:
        logger.error("History upload: completion POST auth fail (401)")
        led_send("LED:AUTH_FAIL\n")
    elif comp_resp.status_code != 200:
        logger.error("History upload: completion POST HTTP %d", comp_resp.status_code)

    uploader.post_json(
        state_url,
        {"device_id": device_id, "device_secret": device_secret, "state": "idle"},
        timeout=10,
    )

    led_send("LED:VEHICLE_IDLE\n")
