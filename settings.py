"""
settings.py
-----------
Manages loading and saving device settings from/to a local JSON file.
"""

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "confidence_threshold": 0.5,
    "api_url": "http://192.168.1.100:5000",
    "command_poll_url": "http://192.168.1.100:5000/api/command",
    "upload_url": "http://192.168.1.100:5000/api/upload",
    "completed_url": "http://192.168.1.100:5000/api/completed",
    "serial_port": "/dev/ttyUSB0",
    "serial_baud": 115200,
    "camera_index": 0,
    "yolo_model_path": "yolo.pt",
    "image_save_dir": "/tmp/weed_captures",
    "device_id": "jetson-nano-01"
}

SETTINGS_FILE = Path("config.json")

# Global lock for thread-safe access
_settings_lock = threading.Lock()
_settings: dict = {}


def load_settings() -> dict:
    """Load settings from JSON file. Falls back to defaults if file is missing."""
    global _settings
    with _settings_lock:
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r") as f:
                    loaded = json.load(f)
                _settings = {**DEFAULT_SETTINGS, **loaded}
                logger.info("Settings loaded from %s", SETTINGS_FILE)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Failed to read settings file: %s. Using defaults.", e)
                _settings = dict(DEFAULT_SETTINGS)
        else:
            logger.info("No settings file found. Creating default config.")
            _settings = dict(DEFAULT_SETTINGS)
            _save_locked()
        return dict(_settings)


def get(key: str, default=None):
    """Thread-safe getter for a single setting value."""
    with _settings_lock:
        return _settings.get(key, default)


def update_settings(new_values: dict) -> bool:
    """Update one or more settings values and persist to JSON file."""
    global _settings
    with _settings_lock:
        _settings.update(new_values)
        return _save_locked()


def _save_locked() -> bool:
    """Write current settings to JSON (must be called while holding _settings_lock)."""
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(_settings, f, indent=2)
        logger.info("Settings saved to %s", SETTINGS_FILE)
        return True
    except IOError as e:
        logger.error("Failed to save settings: %s", e)
        return False


def all_settings() -> dict:
    """Return a copy of the current settings dict."""
    with _settings_lock:
        return dict(_settings)
