"""
AdReel Studio — Video Render Worker v3

Pipeline: TTS → Pexels b-roll → trim+grade+motion → xfade compose → caption burn → Cloudinary
"""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from pipeline import run_render

app = FastAPI(title="AdReel Render Worker v3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

WORKER_SECRET  = os.environ.get("RENDER_WORKER_SECRET", "")
JOBS_FILE      = Path("/tmp/adreel_jobs.json")
JOB_TTL        = 3600 * 6


# ── Job store ─────────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        if JOBS_FILE.exists():
            return json.loads(JOBS_FILE.read_text())
    except Exception:
        pass
    return {}


def _save(jobs: dict):
    try:
        JOBS_FILE.write_text(json.dumps(jobs, indent=2))
    except Exception:
        pass


def _get(job_id: str) -> Optional[dict]:
    return _load().get(job_id)


def _create(job_id: str):
    jobs = _load()
    jobs[job_id] = {
        "id": job_id, "status": "QUEUED", "progress": 0,
        "created_at": time.time(), "updated_at": time.time(),
    }
    _save(jobs)


def _update(job_id: str, **kw):
    jobs   = _load()
    entry  = jobs.get(job_id, {})
    entry.update({"updated_at": time.time(), **kw})
    jobs[job_id] = entry
    # TTL cleanup
    cutoff = time.time() - JOB_TTL
    jobs   = {k: v for k, v in jobs.items() if v.get("created_at", 0) > cutoff}
    _save(jobs)


# ── Auth ──────────────────────────────────────────────────────────────────────
def _verify(secret: str):
    if WORKER_SECRET and secret != WORKER_SECRET:
        raise HTTPException(401, {"ok": False, "error": "Unauthorized"})


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True, "status": "ok", "worker": "adreel-render-worker-v3"}


@app.post("/render/start")
async def render_start(
    body: dict,
    bg: BackgroundTasks,
    x_worker_secret: str = Header(""),
):
    _verify(x_worker_secret)
    plan = body.get("render_plan")
    if not plan:
        raise HTTPException(400, "render_plan required")
    job_id = body.get("job_id") or f"render_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    _create(job_id)
    bg.add_task(run_render, job_id, plan, _update)
    return {"job_id": job_id, "status": "QUEUED"}


@app.get("/render/status")
async def render_status(id: str, x_worker_secret: str = Header("")):
    _verify(x_worker_secret)
    job = _get(id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/render/result")
async def render_result(id: str, x_worker_secret: str = Header("")):
    _verify(x_worker_secret)
    job = _get(id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "DONE":
        raise HTTPException(400, f"Job not done (status={job.get('status')})")
    return job


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
