from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from app.frame_selection import closest_frame as select_closest_frame
from app.frame_selection import select_keyframes
from app.contact_sheet import make_contact_sheet


SNAPSHOT_ROOT = Path(os.getenv("SNAPSHOT_ROOT", "/app/data/snapshots"))
EDGE_API_BASE = os.getenv("EDGE_API_BASE", "http://edge-mcp-server:8010").rstrip("/")
MAX_IMAGE_BYTES = int(os.getenv("VISION_MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
MAX_IMAGE_EDGE = int(os.getenv("VISION_MAX_IMAGE_EDGE", "1280"))
JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "80"))
BUFFER_SECONDS = int(os.getenv("VISION_BUFFER_SECONDS", "12"))
RETENTION_DAYS = int(os.getenv("VISION_RETENTION_DAYS", "7"))
FRAME_OFFSETS_MS = (-2000, -1000, 0, 1000, 2000)
LOCAL_FRAME_OFFSETS_MS = (-1000, 0, 1000)
CAPTURE_OFFSETS_MS = (-2000, -1000, 0, 1000, 2000)
LOCAL_CAPTURE_OFFSETS_MS = (-1000, 0, 1000)
CAPTURE_FILENAMES = {
    -2000: "frame_0001.jpg",
    -1000: "frame_0002.jpg",
    0: "frame_0003.jpg",
    1000: "frame_0004.jpg",
    2000: "frame_0005.jpg",
}
PENDING_CAPTURE_LIMIT = int(os.getenv("VISION_PENDING_CAPTURE_LIMIT", "5"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime:
    if not value:
        return utc_now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class BufferedFrame:
    frame_id: str
    camera_id: str
    elder_id: str
    room: str
    captured_at: datetime
    jpeg: bytes
    width: int
    height: int
    sha256: str


class VisionTrigger(BaseModel):
    elder_id: str = "elder_001"
    camera_id: str
    room: str = "living_room"
    event_type: str
    confidence: float = 0.0
    posture: str = "unknown"
    motion_state: str = "unknown"
    triggered_at: datetime = Field(default_factory=utc_now)
    risk_level: str | None = None


class VisionCaptureRequest(BaseModel):
    elder_id: str = "elder_001"
    camera_id: str = "living_room"
    room: str = "living_room"
    trigger_source: str = "manual"
    reason: str = "capture"


class VisionCaptureClearRequest(BaseModel):
    elder_id: str = "elder_001"
    camera_id: str = "living_room"


buffers: dict[str, deque[BufferedFrame]] = defaultdict(deque)
frame_sets: dict[str, dict[str, Any]] = {}
active_frame_sets: set[str] = set()


def normalize_image(raw: bytes) -> tuple[bytes, int, int]:
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image exceeds 5 MB limit")
    try:
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JPEG/PNG image") from exc
    image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return output.getvalue(), image.width, image.height


def _safe_path_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return cleaned or "default"


def pending_capture_dir(elder_id: str, camera_id: str) -> Path:
    return SNAPSHOT_ROOT / "pending" / _safe_path_part(elder_id) / _safe_path_part(camera_id)


def _camera_urls() -> dict[str, str]:
    raw = os.getenv("VISION_CAMERA_SNAPSHOT_URLS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(key): str(value) for key, value in parsed.items() if value}
        except json.JSONDecodeError:
            pass
    default_url = os.getenv("VISION_CAMERA_SNAPSHOT_URL", "").strip()
    return {"default": default_url} if default_url else {}


def camera_snapshot_url(camera_id: str, room: str) -> str:
    urls = _camera_urls()
    url = urls.get(camera_id) or urls.get(room) or urls.get("default")
    if not url:
        raise HTTPException(status_code=503, detail="camera snapshot url is not configured")
    return url


def pending_capture_metadata_path(image_path: Path) -> Path:
    return image_path.with_suffix(".json")


def read_pending_capture(path: Path) -> dict[str, Any] | None:
    metadata_path = pending_capture_metadata_path(path)
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    metadata["path"] = path
    return metadata


def recent_pending_captures(elder_id: str, camera_id: str, *, limit: int = PENDING_CAPTURE_LIMIT) -> list[dict[str, Any]]:
    directory = pending_capture_dir(elder_id, camera_id)
    captures = [item for item in (read_pending_capture(path) for path in directory.glob("*.jpg")) if item]
    captures.sort(key=lambda item: item.get("captured_at") or "")
    return captures[-limit:]


def trim_pending_captures(elder_id: str, camera_id: str) -> None:
    captures = recent_pending_captures(elder_id, camera_id, limit=1000)
    stale = captures[:-PENDING_CAPTURE_LIMIT]
    for item in stale:
        path = item.get("path")
        if isinstance(path, Path):
            path.unlink(missing_ok=True)
            pending_capture_metadata_path(path).unlink(missing_ok=True)


async def fetch_camera_snapshot(camera_id: str, room: str) -> bytes:
    url = camera_snapshot_url(camera_id, room)
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def public_capture(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "capture_id": item.get("capture_id"),
        "elder_id": item.get("elder_id"),
        "camera_id": item.get("camera_id"),
        "room": item.get("room"),
        "captured_at": item.get("captured_at"),
        "trigger_source": item.get("trigger_source"),
        "reason": item.get("reason"),
        "sha256": item.get("sha256"),
        "width": item.get("width"),
        "height": item.get("height"),
        "original_filename": item.get("original_filename"),
    }


async def create_camera_capture(request: VisionCaptureRequest) -> dict[str, Any]:
    raw = await fetch_camera_snapshot(request.camera_id, request.room)
    jpeg, width, height = normalize_image(raw)
    return save_pending_capture(
        jpeg,
        width,
        height,
        elder_id=request.elder_id,
        camera_id=request.camera_id,
        room=request.room,
        trigger_source=request.trigger_source,
        reason=request.reason,
    )


def save_pending_capture(
    jpeg: bytes,
    width: int,
    height: int,
    *,
    elder_id: str,
    camera_id: str,
    room: str,
    trigger_source: str,
    reason: str,
    original_filename: str | None = None,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    captured_at = captured_at or utc_now()
    capture_id = f"cap_{uuid4().hex[:16]}"
    directory = pending_capture_dir(elder_id, camera_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{captured_at.strftime('%Y%m%dT%H%M%S%fZ')}_{capture_id}.jpg"
    path.write_bytes(jpeg)
    metadata = {
        "capture_id": capture_id,
        "elder_id": elder_id,
        "camera_id": camera_id,
        "room": room,
        "captured_at": captured_at.isoformat(),
        "trigger_source": trigger_source,
        "reason": reason,
        "sha256": hashlib.sha256(jpeg).hexdigest(),
        "width": width,
        "height": height,
    }
    if original_filename:
        metadata["original_filename"] = original_filename
    pending_capture_metadata_path(path).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    trim_pending_captures(elder_id, camera_id)
    return public_capture({**metadata, "path": path})


def prune_buffer(camera_id: str, now: datetime) -> None:
    cutoff = now - timedelta(seconds=BUFFER_SECONDS)
    queue = buffers[camera_id]
    while queue and queue[0].captured_at < cutoff:
        queue.popleft()


def closest_frame(
    camera_id: str,
    target: datetime,
    tolerance_sec: float = 1.4,
    excluded_frame_ids: set[str] | None = None,
) -> BufferedFrame | None:
    return select_closest_frame(
        buffers.get(camera_id, ()),
        target,
        tolerance_sec=tolerance_sec,
        excluded_frame_ids=excluded_frame_ids,
    )


def frame_name(offset_ms: int) -> str:
    if offset_ms < 0:
        return f"frame_m{abs(offset_ms):04d}.jpg"
    if offset_ms > 0:
        return f"frame_p{offset_ms:04d}.jpg"
    return "frame_0000.jpg"


def event_directory(trigger: VisionTrigger, frame_set_id: str) -> Path:
    day = trigger.triggered_at.astimezone(timezone.utc)
    return SNAPSHOT_ROOT / day.strftime("%Y/%m/%d") / trigger.elder_id / frame_set_id


async def send_observation(trigger: VisionTrigger, frame_set_id: str) -> None:
    payload = {
        "elder_id": trigger.elder_id,
        "kind": "vision",
        "source": "vision-service",
        "payload": {
            "elder_id": trigger.elder_id,
            "camera_id": trigger.camera_id,
            "room": trigger.room,
            "event_type": trigger.event_type,
            "confidence": trigger.confidence,
            "posture": trigger.posture,
            "motion_state": trigger.motion_state,
            "risk_level": trigger.risk_level,
            "frame_set_id": frame_set_id,
            "frame_collection_status": "collecting",
            "triggered_at": trigger.triggered_at.isoformat(),
        },
        "observed_at": trigger.triggered_at.isoformat(),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{EDGE_API_BASE}/api/v2/observations", json=payload)
        response.raise_for_status()


async def finalize_frame_set(trigger: VisionTrigger, frame_set_id: str) -> None:
    active_frame_sets.add(frame_set_id)
    try:
        directory = event_directory(trigger, frame_set_id)
        directory.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        image_refs: list[str] = []
        pending = recent_pending_captures(trigger.elder_id, trigger.camera_id)
        if pending:
            selected_pending = pending[-5:]
            while len(selected_pending) < 5:
                selected_pending.insert(0, None)  # type: ignore[arg-type]
            for offset_ms, capture in zip(CAPTURE_OFFSETS_MS, selected_pending, strict=True):
                if capture is None:
                    records.append({"offset_ms": offset_ms, "missing": True})
                    continue
                source_path = capture.get("path")
                if not isinstance(source_path, Path) or not source_path.exists():
                    records.append({"offset_ms": offset_ms, "missing": True, "capture_id": capture.get("capture_id")})
                    continue
                filename = CAPTURE_FILENAMES[offset_ms]
                path = directory / filename
                shutil.copyfile(source_path, path)
                relative = path.relative_to(SNAPSHOT_ROOT).as_posix()
                image_refs.append(relative)
                records.append(
                    {
                        "snapshot_id": capture.get("capture_id"),
                        "capture_id": capture.get("capture_id"),
                        "offset_ms": offset_ms,
                        "sequence_index": len(records) + 1,
                        "filename": filename,
                        "relative_path": relative,
                        "captured_at": capture.get("captured_at"),
                        "target_at": trigger.triggered_at.isoformat(),
                        "sha256": capture.get("sha256"),
                        "width": capture.get("width"),
                        "height": capture.get("height"),
                        "camera_id": capture.get("camera_id"),
                        "room": capture.get("room"),
                        "trigger_source": capture.get("trigger_source"),
                        "reason": capture.get("reason"),
                        "missing": False,
                    }
                )
                source_path.unlink(missing_ok=True)
                pending_capture_metadata_path(source_path).unlink(missing_ok=True)
        else:
            # Compatibility path for older clients that still upload preview frames.
            remaining = (trigger.triggered_at + timedelta(seconds=2.75) - utc_now()).total_seconds()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 5))
            selected_frames = select_keyframes(buffers.get(trigger.camera_id, ()), trigger.triggered_at, FRAME_OFFSETS_MS)
            for offset_ms, frame in zip(FRAME_OFFSETS_MS, selected_frames, strict=True):
                target = trigger.triggered_at + timedelta(milliseconds=offset_ms)
                if frame is None:
                    records.append({"offset_ms": offset_ms, "missing": True, "target_at": target.isoformat()})
                    continue
                filename = frame_name(offset_ms)
                path = directory / filename
                path.write_bytes(frame.jpeg)
                relative = path.relative_to(SNAPSHOT_ROOT).as_posix()
                image_refs.append(relative)
                records.append(
                    {
                        "snapshot_id": frame.frame_id,
                        "offset_ms": offset_ms,
                        "filename": filename,
                        "relative_path": relative,
                        "captured_at": frame.captured_at.isoformat(),
                        "target_at": target.isoformat(),
                        "sha256": frame.sha256,
                        "width": frame.width,
                        "height": frame.height,
                        "camera_id": frame.camera_id,
                        "missing": False,
                    }
                )
        contact_sheet = directory / "contact_sheet.jpg"
        make_contact_sheet(
            records,
            contact_sheet,
            offsets_ms=CAPTURE_OFFSETS_MS,
            cell_width=256,
            cell_height=192,
            jpeg_quality=JPEG_QUALITY,
        )
        local_contact_sheet = directory / "local_contact_sheet.jpg"
        make_contact_sheet(
            records,
            local_contact_sheet,
            offsets_ms=LOCAL_CAPTURE_OFFSETS_MS,
            cell_width=224,
            cell_height=168,
            jpeg_quality=JPEG_QUALITY,
        )
        manifest = {
            "frame_set_id": frame_set_id,
            "elder_id": trigger.elder_id,
            "camera_id": trigger.camera_id,
            "room": trigger.room,
            "event_type": trigger.event_type,
            "triggered_at": trigger.triggered_at.isoformat(),
            "status": "complete" if len(image_refs) == 5 else "partial",
            "capture_mode": "recent_five_captures" if pending else "legacy_frame_buffer",
            "local_frame_policy": "middle_three",
            "frames": records,
            "image_refs": image_refs,
            "contact_sheet_path": contact_sheet.relative_to(SNAPSHOT_ROOT).as_posix(),
            "local_contact_sheet_path": local_contact_sheet.relative_to(SNAPSHOT_ROOT).as_posix(),
            "completed_at": utc_now().isoformat(),
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        frame_sets[frame_set_id] = manifest
    finally:
        active_frame_sets.discard(frame_set_id)


def load_manifest(frame_set_id: str) -> dict[str, Any] | None:
    if frame_set_id in frame_sets:
        return frame_sets[frame_set_id]
    for path in SNAPSHOT_ROOT.glob(f"*/*/*/*/{frame_set_id}/manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            frame_sets[frame_set_id] = manifest
            return manifest
        except (OSError, json.JSONDecodeError):
            return None
    return None


def cleanup_snapshots() -> None:
    cutoff = utc_now() - timedelta(days=RETENTION_DAYS)
    if not SNAPSHOT_ROOT.exists():
        return
    for manifest_path in SNAPSHOT_ROOT.glob("*/*/*/*/*/manifest.json"):
        frame_set_id = manifest_path.parent.name
        if frame_set_id in active_frame_sets:
            continue
        modified = datetime.fromtimestamp(manifest_path.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            shutil.rmtree(manifest_path.parent, ignore_errors=True)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        await asyncio.to_thread(cleanup_snapshots)


@asynccontextmanager
async def lifespan(_: FastAPI):
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Elder Guardian Vision Frame Service", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "vision-service", "buffered_cameras": len(buffers), "retention_days": RETENTION_DAYS}


@app.post("/api/v2/vision/frames")
async def upload_frame(
    image: UploadFile = File(...),
    elder_id: str = Form("elder_001"),
    camera_id: str = Form(...),
    room: str = Form("living_room"),
    captured_at: str | None = Form(None),
) -> dict[str, Any]:
    jpeg, width, height = normalize_image(await image.read())
    timestamp = parse_time(captured_at)
    frame = BufferedFrame(
        frame_id=f"snap_{uuid4().hex[:16]}",
        camera_id=camera_id,
        elder_id=elder_id,
        room=room,
        captured_at=timestamp,
        jpeg=jpeg,
        width=width,
        height=height,
        sha256=hashlib.sha256(jpeg).hexdigest(),
    )
    buffers[camera_id].append(frame)
    prune_buffer(camera_id, timestamp)
    return {"ok": True, "snapshot_id": frame.frame_id, "captured_at": timestamp.isoformat(), "width": width, "height": height}


@app.post("/api/v2/vision/captures")
async def capture_snapshot(request: VisionCaptureRequest) -> dict[str, Any]:
    capture = await create_camera_capture(request)
    return {"ok": True, "capture": capture, "recent": [public_capture(item) for item in recent_pending_captures(request.elder_id, request.camera_id)]}


@app.post("/api/v2/vision/captures/import")
async def import_captures(
    images: list[UploadFile] = File(...),
    elder_id: str = Form("elder_001"),
    camera_id: str = Form("living_room"),
    room: str = Form("living_room"),
) -> dict[str, Any]:
    if len(images) != 5:
        raise HTTPException(status_code=422, detail="exactly five images are required")
    imported: list[dict[str, Any]] = []
    base_time = utc_now()
    for index, image in enumerate(images, start=1):
        raw = await image.read()
        jpeg, width, height = normalize_image(raw)
        imported.append(
            save_pending_capture(
                jpeg,
                width,
                height,
                elder_id=elder_id,
                camera_id=camera_id,
                room=room,
                trigger_source="background_mqtt_import",
                reason=f"imported_five_image_{index}",
                original_filename=image.filename,
                captured_at=base_time + timedelta(milliseconds=index),
            )
        )
        await image.close()
    return {
        "ok": True,
        "elder_id": elder_id,
        "camera_id": camera_id,
        "room": room,
        "imported": imported,
        "recent": [public_capture(item) for item in recent_pending_captures(elder_id, camera_id)],
        "count": len(imported),
    }


@app.get("/api/v2/vision/captures/recent")
async def get_recent_captures(elder_id: str = "elder_001", camera_id: str = "living_room") -> dict[str, Any]:
    captures = [public_capture(item) for item in recent_pending_captures(elder_id, camera_id)]
    return {"ok": True, "elder_id": elder_id, "camera_id": camera_id, "captures": captures, "count": len(captures)}


@app.post("/api/v2/vision/captures/clear")
async def clear_recent_captures(request: VisionCaptureClearRequest) -> dict[str, Any]:
    directory = pending_capture_dir(request.elder_id, request.camera_id)
    count = 0
    if directory.exists():
        for path in directory.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
                count += 1
    return {"ok": True, "elder_id": request.elder_id, "camera_id": request.camera_id, "deleted_files": count}


@app.post("/api/v2/vision/triggers", status_code=202)
async def trigger_event(trigger: VisionTrigger, background_tasks: BackgroundTasks) -> dict[str, Any]:
    if trigger.triggered_at.tzinfo is None:
        trigger = trigger.model_copy(update={"triggered_at": trigger.triggered_at.replace(tzinfo=timezone.utc)})
    frame_set_id = f"frames_{uuid4().hex[:16]}"
    await send_observation(trigger, frame_set_id)
    background_tasks.add_task(finalize_frame_set, trigger, frame_set_id)
    return {"ok": True, "frame_set_id": frame_set_id, "status": "collecting"}


@app.get("/api/v2/vision/events/{frame_set_id}/frames")
async def get_frames(frame_set_id: str) -> dict[str, Any]:
    manifest = load_manifest(frame_set_id)
    if manifest is None:
        if frame_set_id in active_frame_sets:
            return {"frame_set_id": frame_set_id, "status": "collecting", "frames": []}
        raise HTTPException(status_code=404, detail="frame set not found")
    return manifest


@app.get("/api/v2/vision/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: str) -> FileResponse:
    for manifest_path in SNAPSHOT_ROOT.glob("*/*/*/*/*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for frame in manifest.get("frames", []):
            if frame.get("snapshot_id") == snapshot_id and not frame.get("missing"):
                return FileResponse(SNAPSHOT_ROOT / frame["relative_path"], media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="snapshot not found")
