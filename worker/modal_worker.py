"""
Deploy AdReel worker to Modal (serverless GPU).
Usage: modal deploy modal_worker.py
"""
import modal

app_modal = modal.App("adreel-render-worker")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "fonts-liberation", "fonts-noto")
    .pip_install_from_requirements("requirements.txt")
)


@app_modal.function(
    image=image,
    gpu="A10G",
    timeout=600,
    secrets=[modal.Secret.from_name("adreel-secrets")],
)
async def render_reel(job_id: str, render_plan: dict) -> dict:
    """Run render pipeline on Modal GPU."""
    from main import jobs, run_render  # noqa: PLC0415
    jobs[job_id] = {"id": job_id, "status": "QUEUED", "progress": 0}
    await run_render(job_id, render_plan)
    return jobs[job_id]


@app_modal.web_endpoint(method="POST")
async def render_start_modal(body: dict):
    import time  # noqa: PLC0415
    job_id = body.get("job_id", f"modal_{time.time()}")
    render_plan = body.get("render_plan")
    render_reel.spawn(job_id, render_plan)
    return {"job_id": job_id, "status": "QUEUED"}
