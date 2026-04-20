"""
command_poller.py
-----------------
Background thread that polls the web server every 2 seconds for commands.

Supported commands from server:
  - start  { "command": "start", "travel_distance": <float> }
  - stop   { "command": "stop" }
  - update { "command": "update", "settings": { "confidence_threshold": <float>, ... } }
"""

import logging
import threading
import time
from typing import Optional

import requests

import settings
from serial_comm import SerialComm
from weed_detection import WeedDetectionSequence

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0   # seconds


class CommandPoller:
    """Polls the server for commands and dispatches them."""

    def __init__(self, serial: SerialComm):
        self.serial = serial
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._detection_seq: Optional[WeedDetectionSequence] = None
        self._seq_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the polling thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="CommandPollerThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("Command poller started (interval=%s s).", POLL_INTERVAL)

    def stop(self):
        """Stop the polling thread (and any running detection sequence)."""
        self._stop_event.set()
        self._stop_detection()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Command poller stopped.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self._fetch_and_dispatch()
            except Exception as e:
                logger.error("Unexpected error in poll loop: %s", e)
            self._stop_event.wait(POLL_INTERVAL)

    def _fetch_and_dispatch(self):
        url = settings.get("command_poll_url")
        device_id = settings.get("device_id", "jetson-nano-01")
        try:
            resp = requests.get(
                url,
                params={"device_id": device_id},
                timeout=5,
            )
            if resp.status_code == 204:
                # No command pending
                return
            resp.raise_for_status()
        except requests.Timeout:
            logger.debug("Poll timeout — server unreachable.")
            return
        except requests.RequestException as e:
            logger.warning("Poll request failed: %s", e)
            return

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("Received non-JSON poll response.")
            return

        command = payload.get("command", "").lower().strip()
        logger.info("Received command: %s", command)

        if command == "start":
            self._handle_start(payload)
        elif command == "stop":
            self._handle_stop()
        elif command == "update":
            self._handle_update(payload)
        else:
            logger.warning("Unknown command: '%s'", command)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_start(self, payload: dict):
        travel_distance_cm = float(payload.get("travel_distance_cm", 1000.0))
        logger.info("START command received | travel_distance=%.1f cm", travel_distance_cm)

        with self._seq_lock:
            if self._detection_seq and self._detection_seq.is_running():
                logger.warning("Detection sequence already running. Ignoring START.")
                return
            self._detection_seq = WeedDetectionSequence(
                travel_distance_cm=travel_distance_cm,
                serial=self.serial,
            )
            self._detection_seq.start()

    def _handle_stop(self):
        logger.info("STOP command received.")
        self.serial.send_stop()
        self._stop_detection()

    def _handle_update(self, payload: dict):
        new_settings = payload.get("settings", {})
        if not new_settings:
            logger.warning("UPDATE command received with no settings payload.")
            return
        logger.info("UPDATE command received | new_settings=%s", new_settings)
        success = settings.update_settings(new_settings)
        if success:
            logger.info("Settings updated and saved successfully.")
        else:
            logger.error("Failed to save updated settings.")

    def _stop_detection(self):
        with self._seq_lock:
            if self._detection_seq and self._detection_seq.is_running():
                self._detection_seq.stop()
                self._detection_seq.join(timeout=5)
                logger.info("Detection sequence stopped.")
            self._detection_seq = None
