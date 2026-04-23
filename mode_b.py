import logging
import threading
from typing import Callable

import settings
import uploader
from serial_comm import SerialComm
from weed_detection import run_wds

logger = logging.getLogger(__name__)


def run_mode_b(
    *,
    job_id: str,
    travel_distance_cm: float,
    abort_flag: threading.Event,
    serial: SerialComm,
    yolo_model,
    led_send: Callable[[str], None],
    wds_active_flag: threading.Event,
) -> None:
    """SR-24–SR-26: One-Way Weed Detection handler (runs in handler thread)."""
    wds_active_flag.set()
    try:
        result = run_wds(
            forward_distance_cm=travel_distance_cm,
            start_step_index=0,
            job_id=job_id,
            abort_flag=abort_flag,
            serial=serial,
            yolo_model=yolo_model,
            led_send=led_send,
        )
    finally:
        wds_active_flag.clear()

    # SR-26: completion POST
    device_id = settings.get("device_id")
    device_secret = settings.get("device_secret")
    completed_url = settings.get("completed_url")

    if result.status == "completed":
        payload = {
            "job_id": job_id,
            "device_id": device_id,
            "device_secret": device_secret,
            "status": "completed",
            "total_distance_cm": result.distance_covered,
        }
    else:
        payload = {
            "job_id": job_id,
            "device_id": device_id,
            "device_secret": device_secret,
            "status": "aborted",
            "reason": result.reason,
            "total_distance_cm": result.distance_covered,
        }

    resp = uploader.post_json(completed_url, payload, timeout=10)
    if resp is None:
        logger.error("Mode B: completion POST failed (network)")
    elif resp.status_code == 401:
        logger.error("Mode B: completion POST auth fail (401)")
        led_send("LED:AUTH_FAIL\n")
    elif resp.status_code != 200:
        logger.error("Mode B: completion POST HTTP %d", resp.status_code)
