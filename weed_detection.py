"""
weed_detection.py
-----------------
Implements the 8-step weed detection sequence that runs on the Jetson Nano.

Steps:
  1. Capture photo from USB camera.
  2. Run YOLO model to detect weeds.
  3. Draw bounding boxes and save annotated image.
  4. Write detections JSON (coordinates, class label, confidence).
  5. Upload raw image, annotated image, and JSON to server; delete local copies.
  6. Send MOVE command to nodeMCU via serial.
  7. Wait for POSITION_REACHED from nodeMCU.
  8. If total distance covered → send COMPLETED; else repeat from Step 1.
"""

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

import cv2

import settings
import uploader
from serial_comm import SerialComm

logger = logging.getLogger(__name__)

try:
    from ultralytics import YOLO
except ImportError:
    logger.error("ultralytics package not installed. Install it with: pip install ultralytics")
    sys.exit(1)


class WeedDetectionSequence:
    """
    Runs the weed-detection sequence in a dedicated thread.
    Can be stopped safely at any point via stop().
    """

    def __init__(self, travel_distance_cm: float, serial: SerialComm):
        """
        Args:
            travel_distance_cm: Total distance (cm) to travel while scanning.
            serial:             Active SerialComm instance for ESP8266 communication.
        """
        self.travel_distance = travel_distance_cm
        self.serial = serial
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._model = None

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def start(self):
        """Launch the detection sequence in a background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Detection sequence already running.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_sequence,
            name="WeedDetectionThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("Weed detection sequence started (distance=%.1f cm).", self.travel_distance)

    def stop(self):
        """Signal the sequence to stop as soon as possible."""
        self._stop_event.set()
        logger.info("Stop signal sent to weed detection sequence.")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float = None):
        if self._thread:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _stopped(self) -> bool:
        return self._stop_event.is_set()

    def _load_model(self):
        """Load (or reload) the YOLO model."""
        model_path = settings.get("yolo_model_path", "yolo.pt")
        logger.info("Loading YOLO model from %s ...", model_path)
        try:
            self._model = YOLO(model_path)
            logger.info("YOLO model loaded.")
        except Exception as e:
            logger.error("Failed to load YOLO model from '%s': %s", model_path, e)
            sys.exit(1)

    def _ensure_save_dir(self) -> Path:
        save_dir = Path(settings.get("image_save_dir", "/tmp/weed_captures"))
        save_dir.mkdir(parents=True, exist_ok=True)
        return save_dir

    # ------------------------------------------------------------------
    # Detection sequence steps
    # ------------------------------------------------------------------

    def _step1_capture(self, save_dir: Path, capture_id: str) -> Optional[str]:
        """Step 1: Capture a photo from the USB camera."""
        cam_index = settings.get("camera_index", 0)
        raw_path = str(save_dir / f"{capture_id}_raw.jpg")

        cap = cv2.VideoCapture(cam_index)
        if not cap.isOpened():
            logger.error("Step 1: Cannot open camera index %d.", cam_index)
            return None
        ret, frame = cap.read()
        cap.release()
        if not ret:
            logger.error("Step 1: Failed to capture frame.")
            return None
        cv2.imwrite(raw_path, frame)
        logger.info("Step 1: Image captured → %s", raw_path)
        return raw_path

    def _step2_detect(self, raw_image_path: str) -> List[dict]:
        """Step 2: Run YOLO model and return list of detection dicts."""
        confidence_threshold = settings.get("confidence_threshold", 0.5)

        frame = cv2.imread(raw_image_path)
        results = self._model.predict(frame, conf=confidence_threshold, verbose=False)
        detections = []
        for r in results:
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf < confidence_threshold:
                    continue
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                class_id = int(box.cls[0])
                class_label = r.names.get(class_id, str(class_id))
                detections.append({
                    "class_label": class_label,
                    "confidence": round(conf, 4),
                    "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                })
        logger.info("Step 2: %d detection(s) above threshold %.2f.",
                    len(detections), confidence_threshold)
        return detections

    def _step3_annotate(
        self, raw_image_path: str, detections: List[dict], save_dir: Path, capture_id: str
    ) -> str:
        """Step 3: Draw bounding boxes and save annotated image."""
        annotated_path = str(save_dir / f"{capture_id}_annotated.jpg")
        frame = cv2.imread(raw_image_path)

        for det in detections:
            bbox = det["bbox"]
            label = f"{det['class_label']} {det['confidence']:.2f}"
            cv2.rectangle(
                frame,
                (bbox["x1"], bbox["y1"]),
                (bbox["x2"], bbox["y2"]),
                (0, 255, 0), 2,
            )
            cv2.putText(
                frame, label,
                (bbox["x1"], max(bbox["y1"] - 8, 10)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1,
            )

        cv2.imwrite(annotated_path, frame)
        logger.info("Step 3: Annotated image saved → %s", annotated_path)
        return annotated_path

    def _step4_write_json(
        self, detections: List[dict], save_dir: Path, capture_id: str
    ) -> str:
        """Step 4: Write detections to a JSON file."""
        json_path = str(save_dir / f"{capture_id}_detections.json")
        payload = {
            "capture_id": capture_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "device_id": settings.get("device_id", "jetson-nano-01"),
            "detections": detections,
        }
        with open(json_path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info("Step 4: Detections JSON written → %s", json_path)
        return json_path

    def _step5_upload(
        self,
        raw_path: str,
        annotated_path: str,
        json_path: str,
        step_index: int,
    ) -> bool:
        """Step 5: Upload files to server and delete local copies."""
        return uploader.upload_capture(
            upload_url=settings.get("upload_url"),
            raw_image_path=raw_path,
            annotated_image_path=annotated_path,
            detections_json_path=json_path,
            device_id=settings.get("device_id", "jetson-nano-01"),
            step_index=step_index,
        )

    def _step6_send_move(self, step_distance_cm: float) -> bool:
        """Step 6: Send FWD command to ESP8266."""
        logger.info("Step 6: Sending FWD %.1f cm to ESP8266.", step_distance_cm)
        return self.serial.send_move_command("forward", step_distance_cm)

    def _step7_wait_position(self) -> bool:
        """Step 7: Wait for ESP8266 to confirm movement complete."""
        logger.info("Step 7: Waiting for DONE from ESP8266...")
        reached = self.serial.wait_for_done(timeout=120.0)
        if reached:
            logger.info("Step 7: Movement complete.")
        else:
            logger.warning("Step 7: Timed out waiting for DONE.")
        return reached

    def _step8_check_complete(
        self, distance_covered: float
    ) -> bool:
        """Step 8: Return True if total travel distance is covered."""
        return distance_covered >= self.travel_distance

    # ------------------------------------------------------------------
    # Main sequence loop
    # ------------------------------------------------------------------

    def _run_sequence(self):
        self._load_model()

        save_dir = self._ensure_save_dir()
        step_distance = float(settings.get("camera_vision_width_cm", 50.0))
        distance_covered = 0.0
        step_index = 0

        while not self._stopped():
            step_index += 1
            capture_id = uuid.uuid4().hex[:12]
            logger.info("=== Sequence step %d | covered=%.1f/%.1f cm ===",
                        step_index, distance_covered, self.travel_distance)

            # Step 1 – Capture
            if self._stopped():
                break
            raw_path = self._step1_capture(save_dir, capture_id)
            if raw_path is None:
                logger.error("Step 1 failed. Aborting sequence.")
                break

            # Step 2 – Detect
            if self._stopped():
                break
            detections = self._step2_detect(raw_path)

            # Step 3 – Annotate
            if self._stopped():
                break
            annotated_path = self._step3_annotate(raw_path, detections, save_dir, capture_id)

            # Step 4 – Write JSON
            if self._stopped():
                break
            json_path = self._step4_write_json(detections, save_dir, capture_id)

            # Step 5 – Upload
            if self._stopped():
                break
            if not self._step5_upload(raw_path, annotated_path, json_path, step_index):
                logger.warning("Step 5: Upload failed. Continuing sequence.")

            # Step 6 – Move
            if self._stopped():
                break
            remaining = self.travel_distance - distance_covered
            move_dist = min(step_distance, remaining)
            if not self._step6_send_move(move_dist):
                logger.error("Step 6 failed. Aborting sequence.")
                break

            # Step 7 – Wait for position
            if self._stopped():
                break
            if not self._step7_wait_position():
                logger.error("Step 7: Position not reached. Aborting sequence.")
                break

            distance_covered += move_dist

            # Step 8 – Check completion
            if self._step8_check_complete(distance_covered):
                logger.info("Step 8: Total distance covered (%.1f cm). Sending COMPLETED.",
                            distance_covered)
                uploader.send_completed(
                    completed_url=settings.get("completed_url"),
                    device_id=settings.get("device_id", "jetson-nano-01"),
                    total_distance=distance_covered,
                )
                break

        logger.info("Weed detection sequence finished (stopped=%s).", self._stopped())
