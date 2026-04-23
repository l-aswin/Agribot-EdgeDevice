import logging
import threading
import time
from typing import Callable, List, Optional

import requests

import settings
import uploader
from serial_comm import SerialComm

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0

STATE_IDLE = "idle"
STATE_ACTIVE = "active"
STATE_HISTORY = "history"


class CommandPoller:
    """
    Polls the server every 2 s (SR-13/SR-14).
    Dispatches commands subject to gating rules (SR-15c).
    Thread-safe; all shared state accessed under _state_lock.
    """

    def __init__(
        self,
        serial: SerialComm,
        abort_flag: threading.Event,
        polling_stop_event: threading.Event,
        yolo_model,
        wds_active_flag: threading.Event,
        handler_thread_ref: List[Optional[threading.Thread]],
        led_send: Callable[[str], None],
    ) -> None:
        self._serial = serial
        self._abort_flag = abort_flag
        self._polling_stop_event = polling_stop_event
        self._yolo_model = yolo_model
        self._wds_active_flag = wds_active_flag
        self._handler_thread_ref = handler_thread_ref  # list of length 1; mutable ref
        self._led_send = led_send

        self._state = STATE_IDLE
        self._state_lock = threading.Lock()
        self._stop_pending_upload_flag = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._had_connectivity_failure = False

    def start(self) -> None:
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="CommandPollerThread",
            daemon=True,
        )
        self._poll_thread.start()

    def get_thread(self) -> Optional[threading.Thread]:
        return self._poll_thread

    # ------------------------------------------------------------------
    # Poll loop (SR-13, SR-14)
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while True:
            # SR-14: check polling-stop event at start of each cycle
            if self._polling_stop_event.is_set():
                return

            self._poll_once()

            # Wait for next cycle, but exit early if stop event set
            self._polling_stop_event.wait(timeout=POLL_INTERVAL)

    def _poll_once(self) -> None:
        url = settings.get("command_poll_url")
        device_id = settings.get("device_id")
        device_secret = settings.get("device_secret")

        try:
            resp = requests.post(
                url,
                json={"device_id": device_id, "device_secret": device_secret},
                timeout=5,
            )
        except requests.RequestException as exc:
            logger.error("Poll request failed: %s", exc)
            self._led_send("LED:CONNECTIVITY_ERR\n")
            self._had_connectivity_failure = True
            return

        # Connectivity restored
        if self._had_connectivity_failure:
            self._led_send("LED:CONNECTIVITY_OK\n")
            self._had_connectivity_failure = False

        if resp.status_code == 401:
            logger.error("Poll auth fail (401)")
            self._led_send("LED:AUTH_FAIL\n")
            return

        try:
            data = resp.json()
        except Exception:
            logger.warning("Poll response not valid JSON; ignored")
            return

        if not isinstance(data, dict) or "command" not in data:
            logger.warning("Poll response missing 'command' field; ignored")
            return

        command = data.get("command")
        if command is None:
            return
        if command not in ("start", "stop", "update", "start_pending_upload", "stop_pending_upload"):
            logger.warning("Unrecognised command: %r; ignored", command)
            return

        if command in ("start", "update") and not isinstance(data.get("payload"), dict):
            logger.warning("Command %r missing/invalid payload; ignored", command)
            return

        self._dispatch(command, data.get("payload", {}))

    # ------------------------------------------------------------------
    # Dispatch with gating (SR-15c)
    # ------------------------------------------------------------------

    def _dispatch(self, command: str, payload: dict) -> None:
        with self._state_lock:
            state = self._state

        if state == STATE_IDLE:
            self._route(command, payload)
        elif state == STATE_ACTIVE:
            if command in ("start", "start_pending_upload"):
                logger.info("Gated (active): ignoring %r", command)
            elif command == "stop_pending_upload":
                logger.info("No pending upload active; ignoring stop_pending_upload")
            else:
                self._route(command, payload)
        elif state == STATE_HISTORY:
            if command == "stop_pending_upload":
                self._route(command, payload)
            else:
                logger.info("Gated (history): ignoring %r", command)

    def _route(self, command: str, payload: dict) -> None:
        if command == "stop":
            self._handle_stop()
        elif command == "update":
            self._handle_update(payload)
        elif command == "start":
            self._handle_start(payload)
        elif command == "start_pending_upload":
            self._handle_start_pending_upload()
        elif command == "stop_pending_upload":
            self._handle_stop_pending_upload()

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_stop(self) -> None:
        # SR-18a: send STP first, then set abort_flag
        self._serial.send("STP\n")
        self._abort_flag.set()
        logger.info("Stop: STP sent, abort_flag set")

    def _handle_update(self, payload: dict) -> None:
        """SR-16a + SR-17."""
        applied = {}
        blocked = []

        for field, value in payload.items():
            if field not in settings.UPDATABLE_KEYS:
                logger.warning("Update: unrecognised field %r", field)
                blocked.append({"field": field, "reason": "unrecognised_field"})
                continue

            # Validate
            if field == "confidence_threshold":
                if not isinstance(value, (int, float)) or not (0.0 <= float(value) <= 1.0):
                    logger.warning("Update: invalid confidence_threshold %r", value)
                    blocked.append({"field": field, "reason": "validation_error"})
                    continue
                applied[field] = float(value)

            elif field == "camera_vision_width_cm":
                if not isinstance(value, int) or value <= 0:
                    logger.warning("Update: invalid camera_vision_width_cm %r", value)
                    blocked.append({"field": field, "reason": "validation_error"})
                    continue
                # WDS-active guard (item 4 only if item 3 passed)
                if self._wds_active_flag.is_set():
                    logger.warning("Update: camera_vision_width_cm blocked (WDS active)")
                    blocked.append({"field": field, "reason": "wds_active"})
                    continue
                applied[field] = value

        # SR-17: determine status and write
        device_id = settings.get("device_id")
        device_secret = settings.get("device_secret")
        status_url = settings.get("settings_update_status_url")

        if not applied:
            body = {
                "device_id": device_id,
                "device_secret": device_secret,
                "status": "failed",
                "failed": blocked,
            }
        else:
            ok = settings.update_fields(applied)
            if not ok:
                body = {
                    "device_id": device_id,
                    "device_secret": device_secret,
                    "status": "failed",
                    "reason": "disk_write_error",
                }
            elif blocked:
                body = {
                    "device_id": device_id,
                    "device_secret": device_secret,
                    "status": "partial",
                    "applied": list(applied.keys()),
                    "failed": blocked,
                }
            else:
                body = {
                    "device_id": device_id,
                    "device_secret": device_secret,
                    "status": "success",
                }

        resp = uploader.post_json(status_url, body, timeout=5)
        if resp is None:
            logger.error("Update status POST failed (network)")
        elif resp.status_code == 401:
            logger.error("Update status POST auth fail (401)")
            self._led_send("LED:AUTH_FAIL\n")
        elif resp.status_code != 200:
            logger.error("Update status POST HTTP %d", resp.status_code)

    def _handle_start(self, payload: dict) -> None:
        mode = payload.get("mode")
        if mode not in ("A", "B", "C"):
            logger.warning("Start: unrecognised mode %r; ignored", mode)
            return

        if mode == "A":
            logger.info("Mode A: planned — not yet implemented")
            return
        if mode == "C":
            logger.info("Mode C: planned — not yet implemented")
            return

        # Mode B
        job_id = payload.get("job_id")
        travel_distance_cm = payload.get("travel_distance_cm")
        if not job_id or not isinstance(travel_distance_cm, (int, float)):
            logger.warning("Start Mode B: missing job_id or travel_distance_cm")
            return

        self._abort_flag.clear()
        self._set_state(STATE_ACTIVE)

        from mode_b import run_mode_b

        def _handler():
            try:
                run_mode_b(
                    job_id=str(job_id),
                    travel_distance_cm=float(travel_distance_cm),
                    abort_flag=self._abort_flag,
                    serial=self._serial,
                    yolo_model=self._yolo_model,
                    led_send=self._led_send,
                    wds_active_flag=self._wds_active_flag,
                )
            finally:
                self._set_state(STATE_IDLE)

        t = threading.Thread(target=_handler, name="ModeBHandler", daemon=True)
        self._handler_thread_ref[0] = t
        t.start()

    def _handle_start_pending_upload(self) -> None:
        device_id = settings.get("device_id")
        device_secret = settings.get("device_secret")
        state_url = settings.get("device_state_url")

        # SR-49 step 1: POST state = pending_upload (not retried)
        uploader.post_json(
            state_url,
            {"device_id": device_id, "device_secret": device_secret, "state": "pending_upload"},
            timeout=10,
        )

        self._stop_pending_upload_flag.clear()
        self._set_state(STATE_HISTORY)

        from history_upload import run_history_upload

        def _handler():
            try:
                run_history_upload(
                    stop_flag=self._stop_pending_upload_flag,
                    led_send=self._led_send,
                )
            finally:
                self._set_state(STATE_IDLE)

        t = threading.Thread(target=_handler, name="HistoryUploadHandler", daemon=True)
        self._handler_thread_ref[0] = t
        t.start()

    def _handle_stop_pending_upload(self) -> None:
        self._stop_pending_upload_flag.set()
        logger.info("stop_pending_upload: flag set")

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state
            logger.debug("Device state → %s", state)
