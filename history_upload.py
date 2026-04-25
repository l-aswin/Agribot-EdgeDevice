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

        # SR-50: Upload from pending_dir with retry logic from the uploader module.
        # We set move_on_final_fail=False because the files are already in the
        # pending directory; on failure, they should just stay there.
        result = uploader.upload_bundle_with_retry(
            stem=stem,
            job_id=job_id,
            step_index=step_index,
            upload_url=upload_url,
            device_id=device_id,
            device_secret=device_secret,
            image_save_dir=pending_dir,  # Source directory
            pending_uploads_dir=pending_dir,  # Not used, but required by signature
            led_send=led_send,
            stop_flag=stop_flag,
            move_on_final_fail=False,
        )

        if result == "success":
            succeeded += 1
        elif result == "auth_fail":
            # The uploader function already logs the error and sends the LED signal.
            failed += 1
        elif result == "moved":  # This status now means "final failure" for us.
            logger.warning("History upload for stem %s failed after retry, file remains in pending.", stem)
            failed += 1
        elif result == "stopped":
            logger.info("History upload stopped during retry wait for stem %s.", stem)
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
