import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import cv2

import settings
import uploader
from serial_comm import SerialComm

logger = logging.getLogger(__name__)


@dataclass
class WDSResult:
    status: str            # 'completed' | 'aborted'
    reason: Optional[str]  # 'timeout' | 'err' | 'stopped' | None
    distance_covered: float
    steps_taken: int


def _stem_for(step_index: int) -> str:
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    return f"{ts}_{step_index}"


def _delete_partial(stem: str, image_save_dir: Path) -> None:
    uploader.delete_bundle(stem, image_save_dir)


def run_wds(
    *,
    forward_distance_cm: float,
    start_step_index: int,
    job_id: str,
    abort_flag: threading.Event,
    serial: SerialComm,
    yolo_model,
    led_send: Callable[[str], None],
) -> WDSResult:
    """
    Run the Weed Detection Sequence (SR-32–SR-39).

    Reads image_save_dir, pending_uploads_dir, camera_index, camera_vision_width_cm
    from settings at call time. confidence_threshold is read fresh per YOLO inference.
    """
    image_save_dir = Path(settings.get("image_save_dir"))
    pending_uploads_dir = Path(settings.get("pending_uploads_dir"))
    camera_index = settings.get("camera_index")
    camera_vision_width_cm = settings.get("camera_vision_width_cm")
    upload_url = settings.url("upload_url")
    device_id = settings.get("device_id")
    device_secret = settings.get("device_secret")

    distance_covered = 0.0
    step_index = start_step_index

    while distance_covered < forward_distance_cm:
        # SR-18a iteration-start check
        if abort_flag.is_set():
            return WDSResult("aborted", "stopped", distance_covered, step_index - start_step_index)

        stem = _stem_for(step_index)

        # SR-34: Capture
        raw_path = image_save_dir / f"{stem}_raw.jpg"
        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            logger.error("SR-34: Cannot open camera %d", camera_index)
            cap.release()
            return WDSResult("aborted", "err", distance_covered, step_index - start_step_index)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None or frame.size == 0:
            logger.error("SR-34: Empty frame from camera")
            _delete_partial(stem, image_save_dir)
            return WDSResult("aborted", "err", distance_covered, step_index - start_step_index)
        if not cv2.imwrite(str(raw_path), frame):
            logger.error("SR-34: Cannot write raw image %s", raw_path)
            _delete_partial(stem, image_save_dir)
            return WDSResult("aborted", "err", distance_covered, step_index - start_step_index)

        # SR-35: YOLO inference
        confidence_threshold = settings.get("confidence_threshold")
        try:
            results = yolo_model.predict(frame, conf=confidence_threshold, verbose=False)
        except Exception as exc:
            logger.error("SR-35: YOLO inference failed: %s", exc)
            _delete_partial(stem, image_save_dir)
            return WDSResult("aborted", "err", distance_covered, step_index - start_step_index)

        detections: List[dict] = []
        for r in results:
            for box in r.boxes:
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                cls_id = int(box.cls[0])
                detections.append({
                    "class_label": r.names.get(cls_id, str(cls_id)),
                    "confidence": round(conf, 4),
                    "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                })

        # SR-36: Annotate and save
        ann_path = image_save_dir / f"{stem}_annotated.jpg"
        ann_frame = frame.copy()
        for det in detections:
            b = det["bbox"]
            label = f"{det['class_label']} {det['confidence']:.2f}"
            cv2.rectangle(ann_frame, (b["x1"], b["y1"]), (b["x2"], b["y2"]), (0, 255, 0), 2)
            cv2.putText(ann_frame, label, (b["x1"], max(b["y1"] - 8, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        if not cv2.imwrite(str(ann_path), ann_frame):
            logger.error("SR-36: Cannot write annotated image %s", ann_path)
            _delete_partial(stem, image_save_dir)
            return WDSResult("aborted", "err", distance_covered, step_index - start_step_index)

        # SR-37: Write JSON
        jsn_path = image_save_dir / f"{stem}.json"
        try:
            jsn_path.write_text(json.dumps({
                "job_id": job_id,
                "step_index": step_index,
                "detections": detections,
            }))
        except OSError as exc:
            logger.error("SR-37: Cannot write JSON %s: %s", jsn_path, exc)
            _delete_partial(stem, image_save_dir)
            return WDSResult("aborted", "err", distance_covered, step_index - start_step_index)

        # SR-38: Upload with retry; abort-during-retry handled inside uploader
        result = uploader.upload_bundle_with_retry(
            stem=stem,
            job_id=job_id,
            step_index=step_index,
            upload_url=upload_url,
            device_id=device_id,
            device_secret=device_secret,
            image_save_dir=image_save_dir,
            pending_uploads_dir=pending_uploads_dir,
            led_send=led_send,
            stop_flag=abort_flag,
        )
        if result == "stopped":
            return WDSResult("aborted", "stopped", distance_covered, step_index - start_step_index)

        # SR-18a post-upload abort check
        if abort_flag.is_set():
            return WDSResult("aborted", "stopped", distance_covered, step_index - start_step_index)

        # SR-39: Move forward
        move_cm = min(camera_vision_width_cm, forward_distance_cm - distance_covered)
        serial.send(f"FWD:{move_cm:.1f}\n")
        outcome, err_code = serial.wait_for_done_abortable(abort_flag, total_timeout=120.0)

        if outcome == "abort":
            serial.flush_input()
            return WDSResult("aborted", "stopped", distance_covered, step_index - start_step_index)
        if outcome == "timeout":
            serial.send("STP\n")
            serial.flush_input()
            logger.error("SR-39: DONE timeout")
            return WDSResult("aborted", "timeout", distance_covered, step_index - start_step_index)
        if outcome == "err":
            logger.error("SR-39: ESP ERR:%s", err_code)
            return WDSResult("aborted", "err", distance_covered, step_index - start_step_index)

        # outcome == 'done'
        distance_covered += move_cm
        step_index += 1

    return WDSResult("completed", None, distance_covered, step_index - start_step_index)
