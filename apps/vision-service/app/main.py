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
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field

from app.frame_selection import closest_frame as select_closest_frame
from app.frame_selection import select_keyframes


SNAPSHOT_ROOT = Path(os.getenv("SNAPSHOT_ROOT", "/app/data/snapshots"))
EDGE_API_BASE = os.getenv("EDGE_API_BASE", "http://edge-mcp-server:8010").rstrip("/")
MAX_IMAGE_BYTES = int(os.getenv("VISION_MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
MAX_IMAGE_EDGE = int(os.getenv("VISION_MAX_IMAGE_EDGE", "1280"))
JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "80"))
BUFFER_SECONDS = int(os.getenv("VISION_BUFFER_SECONDS", "12"))
RETENTION_DAYS = int(os.getenv("VISION_RETENTION_DAYS", "7"))
FRAME_OFFSETS_MS = (-2000, -1000, 0, 1000, 2000)


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


def make_contact_sheet(records: list[dict[str, Any]], output_path: Path) -> None:
    cell_width, cell_height, label_height = 256, 192, 28
    sheet = Image.new("RGB", (cell_width * 5, cell_height + label_height), "#20252b")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    by_offset = {item["offset_ms"]: item for item in records if not item.get("missing")}
    for index, offset in enumerate(FRAME_OFFSETS_MS):
        x = index * cell_width
        label = f"T{offset / 1000:+g}s" if offset else "T trigger"
        record = by_offset.get(offset)
        if record:
            image = Image.open(output_path.parent / record["filename"]).convert("RGB")
            fitted = ImageOps.contain(image, (cell_width - 8, cell_height - 8))
            px = x + (cell_width - fitted.width) // 2
            py = label_height + (cell_height - fitted.height) // 2
            sheet.paste(fitted, (px, py))
        else:
            draw.rectangle((x + 4, label_height + 4, x + cell_width - 4, label_height + cell_height - 4), outline="#7d8790", width=2)
            draw.text((x + 100, label_height + 85), "missing", fill="#c7cdd3", font=font)
        border = "#ff3b30" if offset == 0 else "#59636d"
        draw.rectangle((x + 1, label_height + 1, x + cell_width - 2, label_height + cell_height - 2), outline=border, width=4 if offset == 0 else 1)
        draw.text((x + 8, 8), label, fill="white", font=font)
    sheet.save(output_path, format="JPEG", quality=JPEG_QUALITY, optimize=True)


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
        # Allow the T+2 upload request to finish before taking the buffer snapshot.
        remaining = (trigger.triggered_at + timedelta(seconds=2.75) - utc_now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(min(remaining, 5))
        directory = event_directory(trigger, frame_set_id)
        directory.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        image_refs: list[str] = []
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
        make_contact_sheet(records, contact_sheet)
        manifest = {
            "frame_set_id": frame_set_id,
            "elder_id": trigger.elder_id,
            "camera_id": trigger.camera_id,
            "room": trigger.room,
            "event_type": trigger.event_type,
            "triggered_at": trigger.triggered_at.isoformat(),
            "status": "complete" if len(image_refs) == 5 else "partial",
            "frames": records,
            "image_refs": image_refs,
            "contact_sheet_path": contact_sheet.relative_to(SNAPSHOT_ROOT).as_posix(),
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
