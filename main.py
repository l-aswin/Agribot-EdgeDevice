import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy
import requests

import settings
from command_poller import CommandPoller
from serial_comm import SerialComm

# ---------------------------------------------------------------------------
# SR-01: Logging — timestamped log file in logs/
# ---------------------------------------------------------------------------
_logs_dir = Path("logs")
_logs_dir.mkdir(exist_ok=True)
_log_filename = _logs_dir / (time.strftime("%d-%b-%Y_%I-%M-%S%p").lower().replace(" ", "") + ".log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_log_filename)),
    ],
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exit_with_error(code: int, message: str) -> None:
    logger.error("Startup error code %d: %s", code, message)
    logger.info("Log written to %s", _log_filename)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Agribot Edge Device — starting up")

    # SR-02: config.json existence
    config_path = Path("config.json")
    if not config_path.exists():
        _exit_with_error(1, "config.json not found")

    # SR-03: JSON parse
    try:
        raw_cfg = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        _exit_with_error(2, f"config.json is not valid JSON: {exc}")

    # SR-04: bulk key-presence check (before value validation)
    missing = [k for k in settings.REQUIRED_KEYS if k not in raw_cfg]
    if missing:
        _exit_with_error(3, f"Missing config keys: {missing}")

    # SR-04: device_id / device_secret must not be empty strings
    for key in ("device_id", "device_secret"):
        if not raw_cfg[key]:
            _exit_with_error(3, f"Config key '{key}' must not be empty — populate it in config.json")

    # SR-04b: value validation (serial_port always available now)
    led_port = raw_cfg["serial_port"]
    baud = int(raw_cfg["serial_baud_rate"])

    if not isinstance(raw_cfg["confidence_threshold"], (int, float)) or \
            not (0.0 <= float(raw_cfg["confidence_threshold"]) <= 1.0):
        _exit_with_error(3, "confidence_threshold must be a float in [0.0, 1.0]")

    if not isinstance(raw_cfg["camera_vision_width_cm"], int) or raw_cfg["camera_vision_width_cm"] <= 0:
        _exit_with_error(3, "camera_vision_width_cm must be a strictly positive integer")

    # SR-06: create required directories
    for dir_key in ("image_save_dir", "pending_uploads_dir"):
        d = Path(raw_cfg[dir_key])
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _exit_with_error(3, f"Cannot create {dir_key} ({d}): {exc}")

    # Initialise settings module
    settings.init(raw_cfg, config_path)

    # SR-07: open serial port
    serial = SerialComm()
    if not serial.open(led_port, baud):
        logger.error("Startup error code 4: cannot open serial port %s", led_port)
        logger.info("Log written to %s", _log_filename)
        sys.exit(4)

    def led_send(frame: str) -> None:
        serial.send(frame)

    # SR-08: PING handshake
    serial.flush_input()
    if not serial.ping(timeout=3.0):
        logger.error("Startup error code 4: ESP8266 did not respond to PING")
        serial.flush_and_close()
        sys.exit(4)
    led_send("LED:STARTUP_INPROGRESS\n")

    # SR-09–SR-11: retry loop — checks repeat every 10 s until all pass
    from ultralytics import YOLO
    yolo_model = None
    while True:
        # SR-09: server connectivity check
        try:
            r = requests.get(
                settings.get("base_api_url") + "/devicecheck",
                params={"device_id": settings.get("device_id"), "device_secret": settings.get("device_secret")},
                timeout=5,
            )
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
        except Exception as exc:
            logger.error("Startup error code 5: server check failed: %s — retrying in 10 s", exc)
            led_send("LED:SERVER_UNREACHABLE\n")
            time.sleep(10)
            continue

        # SR-10: camera self-test
        cam = cv2.VideoCapture(settings.get("camera_index"))
        ret, frame = cam.read()
        cam.release()
        if not ret or frame is None or frame.size == 0:
            logger.error("Startup error code 6: camera self-test failed — retrying in 10 s")
            led_send("LED:CAMERA_FAIL\n")
            time.sleep(10)
            continue

        # SR-11: YOLO self-test
        try:
            yolo_model = YOLO(settings.get("yolo_model_path"))
            yolo_model.predict(numpy.zeros((640, 640, 3), dtype=numpy.uint8), verbose=False)
        except Exception as exc:
            logger.error("Startup error code 7: YOLO self-test failed: %s — retrying in 10 s", exc)
            time.sleep(10)
            continue

        break  # all checks passed

    # SR-12: signal ready
    led_send("LED:STARTUP_OK\n")
    led_send("LED:VEHICLE_IDLE\n")
    logger.info("All startup checks passed; entering polling loop")

    abort_flag = threading.Event()
    polling_stop_event = threading.Event()
    wds_active_flag = threading.Event()
    handler_thread_ref: list = [None]

    poller = CommandPoller(
        serial=serial,
        abort_flag=abort_flag,
        polling_stop_event=polling_stop_event,
        yolo_model=yolo_model,
        wds_active_flag=wds_active_flag,
        handler_thread_ref=handler_thread_ref,
        led_send=led_send,
    )
    poller.start()

    # SR-12 shutdown sequence
    def _on_signal(signum, frame_):
        logger.info("Signal %d received — shutting down", signum)

        # Step 1
        abort_flag.set()
        # Step 2
        polling_stop_event.set()
        # Step 3
        serial.send("STP\n")
        # Step 4
        led_send("LED:SHUTDOWN\n")
        # Step 5
        handler = handler_thread_ref[0]
        if handler and handler.is_alive():
            handler.join(timeout=5)
        # Step 6
        poll_thread = poller.get_thread()
        if poll_thread and poll_thread.is_alive():
            poll_thread.join(timeout=6)
        # Step 7
        serial.flush_and_close()
        logger.info("Shutdown complete. Log: %s", _log_filename)
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Block main thread
    signal.pause()


if __name__ == "__main__":
    main()
