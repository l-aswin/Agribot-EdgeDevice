# Startup Sequence — Agribot Edge Device

**Platform:** NVIDIA Jetson Nano | **Language:** Python

---

## Overview

The startup sequence runs once at boot before the command polling loop begins. Each check is executed in order. If any mandatory check fails, the device logs the error, reports the error code via LED (if serial is available), writes an error log file, and exits.

---

## Error Reporting

- **LED status (via serial):** Send `LED:STARTUP_INPROGRESS` at the start, `LED:STARTUP_OK` on success, `LED:ERR:{code}` on failure.
- Serial is used for LED reporting only if the serial port opens successfully. All errors are also written to a timestamped log file regardless.
- **Log file naming:** `logs/{DD-Mon-YYYY_HH-MM-SSam|pm}.log` (e.g. `logs/22-apr-2026_09-15-30am.log`)

---

## Checks

### Step 0 — Logging & Directories

1. Create `logs/` directory if it does not exist.
2. Open a timestamped log file for this session.
3. All subsequent steps write to this log file.

**Failure:** If log directory cannot be created → print to stderr and exit. No error code (LED not yet available).

---

### Step 1 — Load Settings

1. Check if `config.json` exists in the working directory.
   - If missing → create it with default values and continue (not a fatal error).
2. Parse `config.json` as JSON.
   - If parsing fails → **error code 1** (malformed config).
3. Validate that all required keys are present:
   - `device_id`, `device_secret`,`api_url`, `command_poll_url`, `upload_url`, `completed_url`
   - `serial_port`, `serial_baud_rate`
   - `camera_index`, `camera_vision_width_cm`
   - `yolo_model_path`, `image_save_dir`
   - `confidence_threshold`
   - If any key is missing → **error code 2** (incomplete config).
4. Load values into memory.

**On error codes 1–2:** Serial may not be open yet; attempt to open the hard-coded fallback port (`/dev/ttyUSB0`) to send LED status, then write error log and exit.

---

### Step 2 — Open Serial Port

1. Open the serial port specified in settings (`serial_port`, `serial_baud`).
   - If open fails → **error code 3**.
2. Send `LED:STARTUP_INPROGRESS` to indicate checks are running.
3. Send `PING\n` and wait up to 3 seconds for `OK\n` response.
   - If no response or wrong response → **error code 3**.

**On error code 3:** Send `LED:ERR:3` (using fallback port if primary failed), write error log and exit.

---

### Step 3 — Verify Server Connection

1. Send `GET {api_url}/devicecheck` with query params `device_id` and `device_secret` (from settings).
2. Expect HTTP 200 response within 5 seconds.
   - Any other response or timeout → **error code 4**.

**On error code 4:** Send `LED:ERR:4` via serial, write error log and exit.

---

### Step 4 — Camera Self-Test

1. Open camera at index `camera_index` using OpenCV (`cv2.VideoCapture`).
2. Capture one frame.
3. Verify the frame is not empty and has non-zero dimensions.
4. Release the camera.
5. If any step fails → **error code 5**.

**On error code 5:** Send `LED:ERR:5` via serial, write error log and exit.

---

### Step 5 — YOLO Model Self-Test

1. Load the YOLO model from `yolo_model_path`.
2. Run inference on a blank/test image (e.g. `numpy.zeros((640, 640, 3), dtype=uint8)`).
3. Verify inference completes without exception.
4. If model load or inference fails → **error code 6**.

**On error code 6:** Send `LED:ERR:6` via serial, write error log and exit.

---

### Step 6 — Signal Ready & Start Polling

1. Send `LED:STARTUP_OK` via serial.
2. Log startup success.
3. Start the command polling background thread (polls every 2 seconds).
4. Block main thread, waiting for SIGINT or SIGTERM.
5. On shutdown signal: stop poller, close serial port, exit cleanly.

---

## Error Code Summary

| Code | Stage | Cause |
|------|-------|-------|
| 1 | Settings | `config.json` exists but is not valid JSON |
| 2 | Settings | One or more required config keys are missing |
| 3 | Serial | Serial port failed to open, or ESP8266 did not respond to PING |
| 4 | Server | `/api/devicecheck` did not return HTTP 200 within timeout |
| 5 | Camera | Camera failed to open or returned an empty frame |
| 6 | YOLO | Model failed to load or inference threw an exception |

---

## Sequence Diagram

```
main.py
  │
  ├─ Step 0: Init logging, create logs/ dir
  ├─ Step 1: Load + validate config.json        → error code 1 or 2
  ├─ Step 2: Open serial, PING → OK             → error code 3
  │            └─ Send LED:STARTUP_INPROGRESS
  ├─ Step 3: GET /api/devicecheck               → error code 4
  ├─ Step 4: Camera open + capture test frame   → error code 5
  ├─ Step 5: YOLO model load + inference test   → error code 6
  └─ Step 6: Send LED:STARTUP_OK, start poller, block
```
