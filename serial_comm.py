"""
serial_comm.py
--------------
Handles USB serial communication with the ESP8266 motor controller.

UART Frame Protocol
-------------------
Commands (Jetson Nano → ESP8266):
  FWD:{dist_cm:.1f}\n   — move forward  (e.g. "FWD:45.0\n")
  BWD:{dist_cm:.1f}\n   — move backward (e.g. "BWD:20.0\n")
  TLT:{angle:.1f}\n     — turn left     (e.g. "TLT:90.0\n")
  TRT:{angle:.1f}\n     — turn right    (e.g. "TRT:45.0\n")
  STP\n                 — abort/stop    (e.g. "STP\n")

Responses (ESP8266 → Jetson Nano):
  ACK\n                 — command accepted (sent immediately on receipt)
  DONE\n                — movement complete
  ERR:{code}\n          — error (ERR:UNK = unknown command, ERR:PARAM = bad param)
"""

import logging
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import serial
except ImportError:
    logger.error("pyserial package not installed. Install it with: pip install pyserial")
    sys.exit(1)


class SerialComm:
    """Thread-safe serial communication wrapper for ESP8266 motor controller."""

    # Commands sent TO the ESP8266
    CMD_FWD  = "FWD:{dist:.1f}\n"
    CMD_BWD  = "BWD:{dist:.1f}\n"
    CMD_TLT  = "TLT:{angle:.1f}\n"
    CMD_TRT  = "TRT:{angle:.1f}\n"
    CMD_STOP = "STP\n"

    # Responses received FROM the ESP8266
    RESP_ACK  = "ACK"
    RESP_DONE = "DONE"
    RESP_ERR  = "ERR"

    def __init__(self, port: str, baud: int = 115200, timeout: float = 60.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._serial: Optional[object] = None
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """Open the serial port."""
        try:
            self._serial = serial.Serial(
                self.port, self.baud,
                timeout=self.timeout,
                write_timeout=5.0
            )
            logger.info("Serial connected: %s @ %d baud", self.port, self.baud)
            return True
        except serial.SerialException as e:
            logger.error("Failed to open serial port %s: %s", self.port, e)
            return False

    def disconnect(self):
        """Close the serial port."""
        if self._serial and hasattr(self._serial, "close"):
            self._serial.close()
            logger.info("Serial port closed.")

    def _write(self, frame: str) -> bool:
        """Send a raw frame string. Caller must hold _lock."""
        if not self._serial or not self._serial.is_open:
            logger.error("Serial port not open.")
            return False
        try:
            self._serial.write(frame.encode("utf-8"))
            self._serial.flush()
            logger.info("Serial TX: %s", frame.strip())
            return True
        except serial.SerialException as e:
            logger.error("Serial write error: %s", e)
            return False

    def send_move_command(self, direction: str, distance_cm: float) -> bool:
        """
        Send a move command to the ESP8266.

        Args:
            direction: "forward" or "backward"
            distance_cm: Distance to travel in centimetres.

        Returns:
            True if sent successfully, False otherwise.
        """
        frame = self.CMD_FWD.format(dist=distance_cm) if direction == "forward" \
            else self.CMD_BWD.format(dist=distance_cm)
        with self._lock:
            return self._write(frame)

    def send_turn_command(self, direction: str, angle_deg: float) -> bool:
        """
        Send a turn command to the ESP8266.

        Args:
            direction: "left" or "right"
            angle_deg: Turn angle in degrees.

        Returns:
            True if sent successfully, False otherwise.
        """
        frame = self.CMD_TLT.format(angle=angle_deg) if direction == "left" \
            else self.CMD_TRT.format(angle=angle_deg)
        with self._lock:
            return self._write(frame)

    def send_stop(self) -> bool:
        """
        Send an immediate abort command. Does not wait for DONE.
        Safe to call from any thread — acquires its own lock.
        """
        with self._lock:
            return self._write(self.CMD_STOP)

    def wait_for_done(self, timeout: float = 120.0) -> bool:
        """
        Block until the ESP8266 sends DONE (or ERR), or timeout expires.
        Logs ACK frames as they arrive but only returns on DONE/ERR/timeout.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            True if DONE received, False on ERR, timeout, or port error.
        """
        deadline = time.time() + timeout
        with self._lock:
            if not self._serial or not self._serial.is_open:
                logger.error("Serial port not open.")
                return False
            self._serial.timeout = min(timeout, self.timeout)
            while time.time() < deadline:
                try:
                    line = self._serial.readline().decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    logger.info("Serial RX: %s", line)
                    if line == self.RESP_DONE:
                        return True
                    if line.startswith(self.RESP_ERR):
                        logger.error("ESP8266 error response: %s", line)
                        return False
                    # ACK — command accepted; continue waiting for DONE
                except serial.SerialException as e:
                    logger.error("Serial read error: %s", e)
                    return False
        logger.warning("Timed out waiting for DONE.")
        return False
