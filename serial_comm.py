import logging
import threading
import time
from typing import Optional, Tuple

import serial

logger = logging.getLogger(__name__)


class SerialComm:
    """Thread-safe serial wrapper. Lock is held only per readline/write, not for entire waits."""

    def __init__(self) -> None:
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()

    def open(self, port: str, baud: int) -> bool:
        try:
            self._serial = serial.Serial(port, baud, timeout=0.5)
            logger.info("Serial port opened: %s @ %d", port, baud)
            return True
        except serial.SerialException as exc:
            logger.error("Cannot open serial port %s: %s", port, exc)
            return False

    def close(self) -> None:
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()
                logger.info("Serial port closed.")

    def flush_input(self) -> None:
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.reset_input_buffer()

    def send(self, frame: str) -> bool:
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return False
            try:
                self._serial.write(frame.encode())
                self._serial.flush()
                logger.debug("TX: %s", frame.rstrip())
                return True
            except serial.SerialException as exc:
                logger.error("Serial write error: %s", exc)
                return False

    def _read_line(self) -> str:
        """Acquire lock, read one line (up to 0.5 s timeout), release lock. Returns stripped text."""
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return ""
            try:
                raw = self._serial.readline()
                return raw.decode(errors="ignore").strip()
            except serial.SerialException as exc:
                logger.error("Serial read error: %s", exc)
                return ""

    def ping(self, timeout: float = 3.0) -> bool:
        self.send("PING\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._read_line()
            if line == "OK":
                return True
        return False

    def wait_for_done_abortable(
        self,
        abort_flag: threading.Event,
        total_timeout: float = 120.0,
    ) -> Tuple[str, Optional[str]]:
        """
        Wait for DONE from ESP8266, checking abort_flag between reads.

        Returns:
            ('done', None)           — DONE received
            ('abort', None)          — abort_flag set
            ('timeout', None)        — timed out
            ('err', code_str)        — ERR:{code} received
        """
        deadline = time.monotonic() + total_timeout
        while time.monotonic() < deadline:
            if abort_flag.is_set():
                return ("abort", None)
            line = self._read_line()  # blocks at most 0.5 s
            if abort_flag.is_set():
                return ("abort", None)
            if not line:
                continue
            logger.debug("RX: %s", line)
            if line == "DONE":
                return ("done", None)
            if line == "ACK":
                continue
            if line.startswith("ERR:"):
                code = line[4:]
                logger.error("ESP ERR: %s", code)
                return ("err", code)
        return ("timeout", None)

    def flush_and_close(self) -> None:
        self.flush_input()
        self.close()
