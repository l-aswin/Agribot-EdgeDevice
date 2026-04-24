import json
import threading
from pathlib import Path

_lock = threading.Lock()
_cfg: dict = {}
_config_path: Path = Path("config.json")

REQUIRED_KEYS = [
    "device_id", "device_secret", "base_api_url", "command_poll_url", "upload_url",
    "completed_url", "settings_update_status_url", "device_state_url",
    "history_upload_completed_url", "serial_port", "serial_baud_rate",
    "camera_index", "camera_vision_width_cm", "yolo_model_path",
    "image_save_dir", "pending_uploads_dir", "confidence_threshold",
]

UPDATABLE_KEYS = {"confidence_threshold", "camera_vision_width_cm"}


def init(cfg: dict, config_path: Path) -> None:
    global _config_path
    with _lock:
        _cfg.clear()
        _cfg.update(cfg)
        _config_path = config_path


def get(key: str):
    with _lock:
        return _cfg[key]


def url(endpoint_key: str) -> str:
    """Return base_api_url + the endpoint path stored under endpoint_key."""
    with _lock:
        return _cfg["base_api_url"] + _cfg[endpoint_key]


def update_fields(fields: dict) -> bool:
    """Write fields to disk first, then apply to memory. Returns True on success."""
    with _lock:
        updated = {**_cfg, **fields}
        try:
            tmp = _config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(updated, indent=2))
            tmp.replace(_config_path)
        except OSError:
            return False
        _cfg.update(fields)
        return True
