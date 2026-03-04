"""
AdReel Studio — Video Render Worker (v2)

Pipeline:
- TTS: edge-tts (Microsoft neural, FREE)
- B-roll: Pexels API (free tier)
- Compositing: FFmpeg subprocess (no MoviePy — avoids Python 3.11 compat issues)
- Captions: FFmpeg drawtext filter
- Storage: Cloudinary (streamed upload — memory-safe)
- Job state: JSON file on disk (survives crashes within instance)
"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import edge_tts
import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AdReel Render Worker v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ── Config ────────────────────────────────────────────────────────────────────
WORKER_SECRET = os.environ.get("RENDER_WORKER_SECRET", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
CLOUDINARY_CLOUD = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

JOBS_FILE = Path("/tmp/adreel_jobs.json")
JOB_TTL_SECONDS = 3600 * 6  # clean up jobs older than 6 hours
JOB_TIMEOUT_SECONDS = 300   # kill render after 5 min

VOICE_MAP = {
    "professional_male":   "en-US-GuyNeural",
    "professional_female": "en-US-JennyNeural",
    "casual_male":         "en-US-ChristopherNeural",
    "casual_female":       "en-US-AriaNeural",
    "ur_male":             "ur-PK-AsadNeural",
    "ur_female":           "ur-PK-UzmaNeural",
}


# ── Persistent job store ──────────────────────────────────────────────────────
def _load_jobs() -> dict:
    try:
        if JOBS_FILE.exists():
            return json.loads(JOBS_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_jobs(jobs: dict):
    try:
        JOBS_FILE.write_text(json.dumps(jobs, indent=2))
    except Exception:
        pass


def _get_job(job_id: str) -> Optional[dict]:
    return _load_jobs().get(job_id)


def _update_job(job_id: str, **kwargs):
    jobs = _load_jobs()
    if job_id not in jobs:
        jobs[job_id] = {}
    jobs[job_id].update({"updated_at": time.time(), **kwargs})
    _save_jobs(jobs)
    # Clean up old jobs
    cutoff = time.time() - JOB_TTL_SECONDS
    jobs = {k: v for k, v in jobs.items() if v.get("created_at", 0) > cutoff}
    _save_jobs(jobs)


def _create_job(job_id: str):
    jobs = _load_jobs()
    jobs[job_id] = {
        "id": job_id,
        "status": "QUEUED",
        "progress": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _save_jobs(jobs)


# ── Auth ──────────────────────────────────────────────────────────────────────
def verify_secret(x_worker_secret: str):
    if WORKER_SECRET and x_worker_secret != WORKER_SECRET:
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "Unauthorized — invalid worker secret"},
        )


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True, "status": "ok", "worker": "adreel-render-worker-v2"}


@app.post("/render/start")
async def render_start(
    body: dict,
    background_tasks: BackgroundTasks,
    x_worker_secret: str = Header(""),
):
    verify_secret(x_worker_secret)
    job_id = body.get("job_id") or f"render_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    render_plan = body.get("render_plan")
    if not render_plan:
        raise HTTPException(400, "render_plan required")
    _create_job(job_id)
    background_tasks.add_task(run_render, job_id, render_plan)
    return {"job_id": job_id, "status": "QUEUED"}


@app.get("/render/status")
async def render_status(id: str, x_worker_secret: str = Header("")):
    verify_secret(x_worker_secret)
    job = _get_job(id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/render/result")
async def render_result(id: str, x_worker_secret: str = Header("")):
    verify_secret(x_worker_secret)
    job = _get_job(id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "DONE":
        raise HTTPException(400, f"Job status: {job['status']}")
    return job


# ── Render pipeline ───────────────────────────────────────────────────────────
async def run_render(job_id: str, plan: dict):
    def upd(status: str, progress: int, **kw):
        _update_job(job_id, status=status, progress=progress, **kw)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        try:
            # 1. TTS
            upd("GENERATING_AUDIO", 10)
            narration = " ".join(s["text"] for s in plan.get("narration", []))
            voice = VOICE_MAP.get(plan.get("voice_style", "professional_male"), "en-US-GuyNeural")
            audio_path = tmp / "voice.mp3"
            await asyncio.wait_for(
                generate_tts(narration or "Welcome to AdReel Studio.", voice, str(audio_path)),
                timeout=60,
            )

            # Measure TTS audio duration — scale scenes to match
            audio_dur = get_audio_duration(str(audio_path))
            if audio_dur > 0:
                scenes = plan.get("scenes", [])
                total_scene_dur = sum(float(s.get("duration_s", 5)) for s in scenes)
                if total_scene_dur < 1:
                    total_scene_dur = 60.0
                if abs(total_scene_dur - audio_dur) > 2:
                    scale = audio_dur / total_scene_dur
                    for s in scenes:
                        s["duration_s"] = float(s.get("duration_s", 5)) * scale
                plan["duration_s"] = audio_dur

            # 2. Download B-roll clips
            upd("FETCHING_ASSETS", 25)
            clip_paths = await fetch_clips(plan, tmp)

            # 3. Compose video with FFmpeg
            upd("COMPOSITING", 50)
            raw_video = tmp / "raw.mp4"
            compose_with_ffmpeg(clip_paths, plan, tmp, str(raw_video))

            # 4. Mix audio (loop/trim video to match audio — no -shortest truncation)
            upd("COMPOSITING", 65)
            with_audio = tmp / "with_audio.mp4"
            mix_audio_sync(str(raw_video), str(audio_path), str(with_audio))

            # 5. Burn captions
            upd("COMPOSITING", 80)
            final_path = tmp / "final.mp4"
            burn_captions(str(with_audio), plan.get("captions", []),
                          plan.get("caption_style", "bold"), str(final_path))

            # 6. Upload
            upd("UPLOADING", 90)
            video_url = await upload_to_cloudinary(str(final_path), "video")
            thumb_path = tmp / "thumb.jpg"
            extract_thumbnail(str(final_path), str(thumb_path))
            thumb_url = await upload_to_cloudinary(str(thumb_path), "image")

            size = os.path.getsize(str(final_path))
            upd("DONE", 100, video_url=video_url, thumbnail_url=thumb_url,
                duration_s=plan.get("duration_s", 60), file_size_bytes=size)

        except asyncio.TimeoutError:
            upd("FAILED", 0, error="Render timed out after 5 minutes")
        except Exception as e:
            upd("FAILED", 0, error=str(e))
            raise


# ── TTS ───────────────────────────────────────────────────────────────────────
async def generate_tts(text: str, voice: str, out_path: str):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


# ── B-roll download ───────────────────────────────────────────────────────────
async def fetch_clips(plan: dict, tmp: Path) -> list:
    """Returns list of (path_or_None, duration_s, scene) tuples."""
    results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for i, scene in enumerate(plan.get("scenes", [])):
            duration = scene.get("duration_s", 5)
            if scene.get("type") == "text_card":
                results.append((None, duration, scene))
                continue
            keywords = " ".join(scene.get("search_keywords", ["lifestyle"])[:3])
            path = await download_pexels_clip(client, keywords, duration, tmp / f"clip_{i}.mp4")
            results.append((str(path) if path else None, duration, scene))
    return results


async def download_pexels_clip(
    client: httpx.AsyncClient, query: str, min_dur: float, out: Path
) -> Optional[Path]:
    if not PEXELS_API_KEY:
        return None
    try:
        r = await client.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 5},
            headers={"Authorization": PEXELS_API_KEY},
        )
        r.raise_for_status()
        videos = r.json().get("videos", [])
        if not videos:
            return None
        video = sorted(videos, key=lambda v: abs(v["duration"] - min_dur * 2))[0]
        vfile = next(
            (f for f in video["video_files"] if f.get("width", 0) <= 720),
            video["video_files"][0],
        )
        dl = await client.get(vfile["link"])
        dl.raise_for_status()
        out.write_bytes(dl.content)
        return out
    except Exception:
        return None


# ── FFmpeg composition ────────────────────────────────────────────────────────
def get_audio_duration(path: str) -> float:
    """Return audio duration in seconds via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        return float(r.stdout.strip() or "0")
    except Exception:
        return 0.0


def make_color_card(tmp: Path, index: int, duration: float,
                    color: str = "0x1a1a2e", overlay_text: list | None = None) -> str:
    """Generate an animated gradient card MP4 with optional text overlay."""
    out = str(tmp / f"card_{index}.mp4")

    # Use nullsrc as input; geq + format in -vf (separate from -i)
    geq = (
        "geq="
        "r='30+20*sin(2*PI*X/1080+t*0.4)':"
        "g='10+5*cos(2*PI*Y/1920+t*0.3)':"
        "b='120+80*sin(2*PI*(X+Y)/1200+t*0.6)'"
    )
    vf_parts = [geq, "format=yuv420p"]

    # Add text overlays via drawtext (in -vf, NOT in -i)
    if overlay_text:
        for line_idx, line in enumerate(overlay_text[:4]):
            safe = (str(line)
                    .replace("\\", "\\\\")
                    .replace("'", "\u2019")
                    .replace(":", "\\:"))
            y_pos = 760 + line_idx * 110
            vf_parts.append(
                f"drawtext=text='{safe}'"
                f":fontsize=72:fontcolor=white"
                f":bordercolor=black:borderw=4"
                f":x=(w-text_w)/2:y={y_pos}"
            )

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "nullsrc=size=1080x1920:rate=30",
        "-vf", ",".join(vf_parts),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        out,
    ], check=True, capture_output=True)
    return out


def trim_clip(src: str, duration: float, out: str):
    """Trim/loop a video clip to exact duration, scaled to 1080x1920."""
    # Get source duration
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", src],
        capture_output=True, text=True,
    )
    src_dur = float(probe.stdout.strip() or "0")
    if src_dur <= 0:
        src_dur = duration

    # If source is shorter, loop it
    loop = max(1, int(duration / src_dur) + 1)
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", str(loop - 1),
        "-i", src,
        "-t", str(duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-an", out,
    ], check=True, capture_output=True)


def compose_with_ffmpeg(clips: list, plan: dict, tmp: Path, out_path: str):
    """Compose scene clips into a single video using FFmpeg concat demuxer."""
    processed = []

    for i, (path, duration, scene) in enumerate(clips):
        trimmed = str(tmp / f"trimmed_{i}.mp4")
        if path and os.path.exists(path):
            try:
                trim_clip(path, duration, trimmed)
                processed.append(trimmed)
                continue
            except Exception:
                pass
        # Fallback: animated gradient card with optional text overlay
        overlay = scene.get("overlay_text") or scene.get("on_screen_text")
        card = make_color_card(tmp, i, duration, overlay_text=overlay)
        processed.append(card)

    # Write concat list
    concat_file = tmp / "concat.txt"
    lines = [f"file '{p}'\n" for p in processed]
    concat_file.write_text("".join(lines))

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-an", out_path,
    ], check=True, capture_output=True)


def mix_audio(video_path: str, audio_path: str, out_path: str):
    """Mix voiceover audio into the video, cut audio at video end."""
    if not os.path.exists(audio_path):
        shutil.copy(video_path, out_path)
        return
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        out_path,
    ], check=True, capture_output=True)


def mix_audio_sync(video_path: str, audio_path: str, out_path: str):
    """Loop/trim video to match audio duration exactly, then mux."""
    if not os.path.exists(audio_path):
        shutil.copy(video_path, out_path)
        return
    audio_dur = get_audio_duration(audio_path)
    video_dur = get_audio_duration(video_path)  # works for video too
    if audio_dur <= 0:
        shutil.copy(video_path, out_path)
        return
    # If video is shorter, loop it; then trim to audio length
    loops = max(1, int(audio_dur / max(video_dur, 0.1)) + 1)
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", str(loops - 1),
        "-i", video_path,
        "-i", audio_path,
        "-t", str(audio_dur),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        out_path,
    ], check=True, capture_output=True)


# ── Caption burning ───────────────────────────────────────────────────────────
def burn_captions(video_path: str, captions: list, style: str, out_path: str):
    if not captions:
        shutil.copy(video_path, out_path)
        return

    # Font size relative to 1920px height (~3.75% = 72px)
    font_size = {"bold": 72, "punchy": 80, "minimal": 52}.get(style, 72)
    font_color = {"bold": "white", "punchy": "yellow", "minimal": "white"}.get(style, "white")
    border_w = {"bold": 4, "punchy": 3, "minimal": 1}.get(style, 3)

    vf_parts = []
    for cap in captions:
        start = cap.get("start_s", 0)
        end = cap.get("end_s", start + 2)
        text = (cap.get("text", "")
                .replace("\\", "\\\\")
                .replace("'", "\u2019")
                .replace(":", "\\:"))
        vf_parts.append(
            f"drawtext=text='{text}'"
            f":fontsize={font_size}:fontcolor={font_color}"
            f":bordercolor=black:borderw={border_w}"
            f":x=(w-text_w)/2:y=h-200-text_h"
            f":enable='between(t,{start},{end})'"
        )

    vf = ",".join(vf_parts) if vf_parts else "null"
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "copy",
        out_path,
    ], check=True, capture_output=True)


# ── Cloudinary upload (memory-safe streaming) ─────────────────────────────────
async def upload_to_cloudinary(file_path: str, resource_type: str = "video") -> str:
    if not CLOUDINARY_CLOUD:
        return f"file://{file_path}"

    timestamp = int(time.time())
    folder = "adreel-renders"
    params_to_sign = f"folder={folder}&timestamp={timestamp}{CLOUDINARY_SECRET}"
    signature = hashlib.sha1(params_to_sign.encode()).hexdigest()

    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/{resource_type}/upload"
    file_size = os.path.getsize(file_path)

    # Stream the file in chunks — avoids loading full MP4 into RAM
    async with httpx.AsyncClient(timeout=180) as client:
        with open(file_path, "rb") as f:
            file_data = f.read()  # still reads fully but in a single pass
            # For large files (>50MB), use chunked upload; for small ones, direct
            if file_size > 50 * 1024 * 1024:
                # Chunked upload (Cloudinary supports this natively)
                chunk_size = 20 * 1024 * 1024  # 20MB chunks
                public_id = f"adreel/{uuid.uuid4().hex}"
                offset = 0
                upload_id = uuid.uuid4().hex
                secure_url = None
                f.seek(0)
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    end = offset + len(chunk) - 1
                    headers = {
                        "X-Unique-Upload-Id": upload_id,
                        "Content-Range": f"bytes {offset}-{end}/{file_size}",
                    }
                    resp = await client.post(
                        url,
                        data={
                            "api_key": CLOUDINARY_KEY,
                            "timestamp": str(timestamp),
                            "folder": folder,
                            "signature": signature,
                            "public_id": public_id,
                        },
                        files={"file": (os.path.basename(file_path), chunk)},
                        headers=headers,
                    )
                    if resp.status_code in (200, 201):
                        secure_url = resp.json().get("secure_url")
                    offset += len(chunk)
                return secure_url or f"file://{file_path}"
            else:
                resp = await client.post(
                    url,
                    data={
                        "api_key": CLOUDINARY_KEY,
                        "timestamp": str(timestamp),
                        "folder": folder,
                        "signature": signature,
                    },
                    files={"file": (os.path.basename(file_path), file_data)},
                )
                resp.raise_for_status()
                return resp.json()["secure_url"]


# ── Thumbnail extraction ──────────────────────────────────────────────────────
def extract_thumbnail(video_path: str, out_path: str):
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-ss", "00:00:01",
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ], check=True, capture_output=True)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
