"""
main.py
-------
Entry point for the Jetson Nano Weed Detection System.

Startup sequence:
  1. Configure logging.
  2. Load settings from config.json.
  3. Open serial connection to nodeMCU.
  4. Start the command-polling thread.
  5. Block until interrupted (Ctrl-C or SIGTERM).
  6. Graceful shutdown.
"""

import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import settings
from command_poller import CommandPoller
from serial_comm import SerialComm

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

_logs_dir = Path("logs")
_logs_dir.mkdir(exist_ok=True)
_log_filename = _logs_dir / (datetime.now().strftime("%d-%B-%Y_%I-%M-%S%p").lower() + ".log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%d-%b-%Y %I:%M:%S %p",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_log_filename),
    ],
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("=" * 60)
    logger.info("  Jetson Nano Weed Detection System — Starting Up")
    logger.info("=" * 60)

    # Step 1: Load settings
    cfg = settings.load_settings()
    logger.info("Config loaded: %s", cfg)

    # Step 2: Open serial port
    serial = SerialComm(
        port=cfg["serial_port"],
        baud=cfg["serial_baud"],
    )
    if not serial.connect():
        logger.warning("Serial port unavailable — continuing in simulation mode.")

    # Step 3: Start command poller
    poller = CommandPoller(serial=serial)
    poller.start()

    # Step 4: Handle shutdown signals gracefully
    shutdown_requested = {"flag": False}

    def _shutdown(signum, frame):
        logger.info("Shutdown signal received (%s). Stopping...", signum)
        shutdown_requested["flag"] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("System running. Press Ctrl-C to stop.")
    try:
        while not shutdown_requested["flag"]:
            time.sleep(0.5)
    finally:
        logger.info("Shutting down...")
        poller.stop()
        serial.disconnect()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
