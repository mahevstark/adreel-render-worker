"""
AdReel Studio — Video Render Worker
Converts a RenderPlan JSON into a real 9:16 MP4 reel.

Stack:
- TTS: edge-tts (free, Microsoft neural voices, no API key needed)
- B-roll: Pexels API (free tier: 200 req/hour)
- Compositing: MoviePy + FFmpeg
- Captions: FFmpeg drawtext filter (burned in)
- Storage: Cloudflare R2 via boto3 (S3-compatible)
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import boto3
import edge_tts
import httpx
import numpy as np
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from moviepy.editor import (
    AudioFileClip, ColorClip, VideoFileClip, concatenate_videoclips,
)
from PIL import Image  # noqa: F401 (keep Pillow import for future use)

app = FastAPI(title="AdReel Render Worker")

# ── CORS — allow Vercel frontend + any server-to-server calls ─────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Vercel origin set at infra level; worker is server-to-server
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ── Health check — Railway uses this to mark deployment "Healthy" ─────────────
@app.get("/health")
async def health():
    return {"ok": True, "status": "ok", "worker": "adreel-render-worker"}

WORKER_SECRET = os.environ.get("RENDER_WORKER_SECRET", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "adreel-renders")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")

jobs: dict = {}

VOICE_MAP = {
    "professional_male":   "en-US-GuyNeural",
    "professional_female": "en-US-JennyNeural",
    "casual_male":         "en-US-ChristopherNeural",
    "casual_female":       "en-US-AriaNeural",
    "ur_male":             "ur-PK-AsadNeural",
    "ur_female":           "ur-PK-UzmaNeural",
}


def verify_secret(x_worker_secret: str = Header(default="")):
    if WORKER_SECRET and x_worker_secret != WORKER_SECRET:
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "Unauthorized — invalid worker secret"},
        )


@app.post("/render/start")
async def render_start(body: dict, background_tasks: BackgroundTasks,
                       x_worker_secret: str = Header("")):
    verify_secret(x_worker_secret)
    job_id = body.get("job_id") or f"render_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    render_plan = body.get("render_plan")
    if not render_plan:
        raise HTTPException(400, "render_plan required")
    jobs[job_id] = {"id": job_id, "status": "QUEUED", "progress": 0,
                    "created_at": time.time(), "updated_at": time.time()}
    background_tasks.add_task(run_render, job_id, render_plan)
    return {"job_id": job_id, "status": "QUEUED"}


@app.get("/render/status")
async def render_status(id: str, x_worker_secret: str = Header("")):
    verify_secret(x_worker_secret)
    job = jobs.get(id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/render/result")
async def render_result(id: str, x_worker_secret: str = Header("")):
    verify_secret(x_worker_secret)
    job = jobs.get(id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "DONE":
        raise HTTPException(400, f"Job status: {job['status']}")
    return job


# ─── Main render pipeline ─────────────────────────────────────────────────────
async def run_render(job_id: str, plan: dict):
    def update(status: str, progress: int, **kwargs):
        jobs[job_id].update({"status": status, "progress": progress,
                             "updated_at": time.time(), **kwargs})

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        try:
            update("FETCHING_ASSETS", 5)
            scene_clips = await fetch_scene_clips(plan, tmp)

            update("GENERATING_AUDIO", 25)
            narration_text = " ".join(s["text"] for s in plan.get("narration", []))
            voice = VOICE_MAP.get(plan.get("voice_style", "professional_male"), "en-US-GuyNeural")
            audio_path = tmp / "voiceover.mp3"
            await generate_tts(narration_text or "Welcome.", voice, str(audio_path))

            update("COMPOSITING", 40)
            video_path = tmp / "reel_raw.mp4"
            compose_video(scene_clips, str(audio_path), plan, str(video_path))

            update("COMPOSITING", 75)
            final_path = tmp / "reel_final.mp4"
            burn_captions(str(video_path), plan.get("captions", []),
                          plan.get("caption_style", "bold"), str(final_path))

            update("UPLOADING", 90)
            key = f"renders/{job_id}/reel.mp4"
            thumb_key = f"renders/{job_id}/thumb.jpg"
            video_url = upload_to_r2(str(final_path), key)

            thumb_path = tmp / "thumb.jpg"
            extract_thumbnail(str(final_path), str(thumb_path))
            thumb_url = upload_to_r2(str(thumb_path), thumb_key)

            file_size = os.path.getsize(str(final_path))
            update("DONE", 100, video_url=video_url, thumbnail_url=thumb_url,
                   duration_s=plan.get("duration_s", 60), file_size_bytes=file_size)
        except Exception as e:
            update("FAILED", 0, error=str(e))
            raise


# ─── Asset fetching ───────────────────────────────────────────────────────────
async def fetch_scene_clips(plan: dict, tmp: Path) -> list:
    clips = []
    async with httpx.AsyncClient(timeout=30) as client:
        for i, scene in enumerate(plan.get("scenes", [])):
            keywords = " ".join(scene.get("search_keywords", ["lifestyle"])[:3])
            duration = scene.get("duration_s", 5)
            if scene.get("type") == "text_card":
                clips.append({"type": "text_card", "path": None, "duration": duration, "scene": scene})
                continue
            clip_path = await download_pexels_video(client, keywords, duration, tmp / f"clip_{i}.mp4")
            clips.append({"type": "broll", "path": str(clip_path) if clip_path else None,
                          "duration": duration, "scene": scene})
    return clips


async def download_pexels_video(client: httpx.AsyncClient, query: str,
                                 min_duration: float, out_path: Path) -> Optional[Path]:
    if not PEXELS_API_KEY:
        return None
    try:
        resp = await client.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 5},
            headers={"Authorization": PEXELS_API_KEY},
        )
        resp.raise_for_status()
        videos = resp.json().get("videos", [])
        if not videos:
            return None
        video = sorted(videos, key=lambda v: abs(v["duration"] - min_duration * 2))[0]
        video_file = next(
            (f for f in video["video_files"] if f.get("width", 0) <= 720 and f.get("quality") in ("hd", "sd")),
            video["video_files"][0],
        )
        dl = await client.get(video_file["link"])
        dl.raise_for_status()
        out_path.write_bytes(dl.content)
        return out_path
    except Exception:
        return None


# ─── TTS ──────────────────────────────────────────────────────────────────────
async def generate_tts(text: str, voice: str, out_path: str):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


# ─── Video composition ────────────────────────────────────────────────────────
def compose_video(scene_clips: list, audio_path: str, plan: dict, out_path: str):
    W, H, fps = 1080, 1920, 30
    clips = []

    for item in scene_clips:
        duration = item["duration"]
        scene = item["scene"]
        if item["path"] and os.path.exists(item["path"]):
            try:
                clip = VideoFileClip(item["path"])
                clip_ratio = clip.w / clip.h
                if clip_ratio > W / H:
                    clip = clip.crop(x_center=clip.w / 2, width=int(clip.h * W / H))
                clip = clip.resize((W, H))
                if clip.duration < duration:
                    loops = int(np.ceil(duration / clip.duration))
                    clip = concatenate_videoclips([clip] * loops)
                clip = clip.subclip(0, duration)
                motion = scene.get("motion", "static")
                if motion == "zoom_in":
                    clip = clip.resize(lambda t: 1 + 0.04 * t / duration)  # type: ignore[arg-type]
                elif motion == "zoom_out":
                    clip = clip.resize(lambda t: 1.15 - 0.04 * t / duration)  # type: ignore[arg-type]
                clips.append(clip)
            except Exception:
                clips.append(make_color_card(W, H, duration))
        else:
            clips.append(make_color_card(W, H, duration))

    if not clips:
        clips = [make_color_card(W, H, 60)]

    final = concatenate_videoclips(clips, method="compose")
    if final.duration < 60:
        final = concatenate_videoclips([final, make_color_card(W, H, 60 - final.duration)], method="compose")

    if os.path.exists(audio_path):
        audio = AudioFileClip(audio_path)
        final = final.set_audio(audio if audio.duration <= final.duration else audio.subclip(0, final.duration))

    final.write_videofile(out_path, fps=fps, codec="libx264", audio_codec="aac",
                          bitrate="4000k", preset="fast", threads=4, logger=None)


def make_color_card(w: int, h: int, duration: float) -> ColorClip:
    return ColorClip(size=(w, h), color=(26, 26, 46), duration=duration)


# ─── Caption burning ──────────────────────────────────────────────────────────
def burn_captions(video_path: str, captions: list, style: str, out_path: str):
    if not captions:
        shutil.copy(video_path, out_path)
        return
    font_size = {"bold": 72, "punchy": 80, "minimal": 52}.get(style, 72)
    font_color = {"bold": "white", "punchy": "yellow", "minimal": "white"}.get(style, "white")
    border_w = {"bold": 4, "punchy": 3, "minimal": 1}.get(style, 3)
    filters = []
    for cap in captions:
        start = cap.get("start_s", 0)
        end = cap.get("end_s", start + 2)
        text = cap.get("text", "").replace("'", "\\'").replace(":", "\\:")
        filters.append(
            f"drawtext=text='{text}':fontsize={font_size}:fontcolor={font_color}"
            f":bordercolor=black:borderw={border_w}"
            f":x=(w-text_w)/2:y=h-200-text_h"
            f":enable='between(t,{start},{end})'"
        )
    filter_str = ",".join(filters) if filters else "null"
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vf", filter_str,
         "-c:v", "libx264", "-c:a", "copy", "-preset", "fast", out_path],
        check=True, capture_output=True,
    )


# ─── R2 Upload ────────────────────────────────────────────────────────────────
def upload_to_r2(file_path: str, key: str) -> str:
    if not R2_ENDPOINT:
        return f"file://{file_path}"
    s3 = boto3.client("s3", endpoint_url=R2_ENDPOINT,
                      aws_access_key_id=R2_ACCESS_KEY,
                      aws_secret_access_key=R2_SECRET_KEY, region_name="auto")
    content_type = "video/mp4" if key.endswith(".mp4") else "image/jpeg"
    s3.upload_file(file_path, R2_BUCKET, key,
                   ExtraArgs={"ContentType": content_type, "ACL": "public-read"})
    return f"{R2_PUBLIC_URL}/{key}"


def extract_thumbnail(video_path: str, out_path: str):
    clip = VideoFileClip(video_path)
    clip.save_frame(out_path, t=1.0)
    clip.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
