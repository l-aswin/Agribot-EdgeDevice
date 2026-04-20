# Jetson Nano Weed Detection System

A Python-based edge AI system for autonomous weed detection and rover control, designed to run on a **Jetson Nano**. The system polls a web server for commands, runs a YOLO model on USB camera frames to detect weeds, uploads annotated results, and drives a nodeMCU-based rover via USB serial.

---

## Architecture

```
main.py
 ├── settings.py          — JSON-backed config (thread-safe read/write)
 ├── command_poller.py    — HTTP polling thread (every 2 s)
 │    └── weed_detection.py — 8-step detection + movement sequence
 │         ├── serial_comm.py  — USB serial ↔ nodeMCU
 │         └── uploader.py     — HTTP multipart file upload
 └── config.json          — Persisted settings
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

> **Jetson Nano note:** For GPU-accelerated OpenCV and PyTorch, use NVIDIA JetPack wheels rather than the standard pip packages.

### 2. Place your YOLO model

Copy your trained model file to the project directory:

```bash
cp /path/to/your/model.pt ./yolo.pt
```

### 3. Edit `config.json`

Update the server URLs and serial port to match your environment:

```json
{
  "api_url":           "http://<server-ip>:5000",
  "command_poll_url":  "http://<server-ip>:5000/api/command",
  "upload_url":        "http://<server-ip>:5000/api/upload",
  "completed_url":     "http://<server-ip>:5000/api/completed",
  "serial_port":       "/dev/ttyUSB0",
  "yolo_model_path":   "yolo.pt",
  "confidence_threshold": 0.5,
  "device_id":         "jetson-nano-01"
}
```

### 4. Run

```bash
python main.py
```

---

## Server API Contract

The device expects the following HTTP endpoints on the web server:

### `GET /api/command?device_id=<id>`

| Response | Meaning |
|----------|---------|
| `204 No Content` | No pending command |
| `200 {"command":"start","travel_distance":10.0}` | Start sequence |
| `200 {"command":"stop"}` | Stop sequence |
| `200 {"command":"update","settings":{"confidence_threshold":0.7}}` | Update settings |

### `POST /api/upload` (multipart/form-data)

Fields: `device_id`, `step_index`, `raw_image`, `annotated_image`, `detections` (JSON file).

### `POST /api/completed` (JSON body)

```json
{ "device_id": "jetson-nano-01", "status": "completed", "total_distance": 10.0 }
```

---

## nodeMCU Serial Protocol

| Direction | Message | Meaning |
|-----------|---------|---------|
| Jetson → MCU | `MOVE:1.500\n` | Move forward 1.5 m |
| MCU → Jetson | `POSITION_REACHED` | Movement complete |

---

## Weed Detection Sequence (per step)

```
Step 1 → Capture frame from USB camera
Step 2 → Run YOLO inference (filtered by confidence_threshold)
Step 3 → Draw bounding boxes → save annotated image
Step 4 → Write detections JSON (coords, class, confidence)
Step 5 → Upload raw + annotated image + JSON → delete local copies
Step 6 → Send MOVE command to nodeMCU
Step 7 → Wait for POSITION_REACHED from nodeMCU
Step 8 → If distance covered → send COMPLETED, else → repeat
```

---

## Logs

Logs are written to both **stdout** and `weed_detection.log` in the project directory.
