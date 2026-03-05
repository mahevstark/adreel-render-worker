"""
modal_worker.py — Mode A: CogVideoX-2B on Modal.com serverless GPU
──────────────────────────────────────────────────────────────────────────────
⚠️  HONEST GPU REQUIREMENTS:
    CogVideoX-2B  → 14 GB VRAM minimum (A10G works, T4 will OOM)
    CogVideoX1.5-5B → 24 GB VRAM (A100 40GB recommended)
    Railway CPU free tier: 0 GB VRAM — CANNOT run this. Don't try.

COST ESTIMATE (Modal.com):
    A10G (24GB):   ~$0.00059/GPU-second
    6 clips × ~90s render each = ~540 GPU-seconds
    Cost per 60s video = ~$0.32
    Free credits: $30 = ~94 free videos

SETUP:
    1. `pip install modal`
    2. `modal token new` (one-time auth)
    3. `modal deploy worker/modal_worker.py`
    4. Set MODAL_ENDPOINT=<your-modal-web-url> in Railway env vars
    5. Set RENDER_MODE=ai in Railway env vars

DEPLOY:
    cd adreel-worker
    modal deploy worker/modal_worker.py
"""
import modal

app   = modal.App("adreel-cogvideo")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "diffusers>=0.31.0",
        "torch>=2.2.0",
        "torchvision",
        "transformers>=4.40.0",
        "accelerate>=0.30.0",
        "imageio[ffmpeg]>=2.34.0",
        "fastapi[standard]>=0.115.0",
        "sentencepiece",
    )
)

# Persist model weights across cold starts (~14GB download once)
model_volume = modal.Volume.from_name("cogvideo-weights", create_if_missing=True)


@app.cls(
    gpu="A10G",
    image=image,
    volumes={"/root/model_cache": model_volume},
    timeout=600,
    allow_concurrent_inputs=1,
)
class CogVideoWorker:
    @modal.enter()
    def load(self):
        import os, torch
        from diffusers import CogVideoXPipeline

        os.environ["HF_HOME"] = "/root/model_cache"
        print("[CogVideoX] Loading model CogVideoX-2b ...")
        self.pipe = CogVideoXPipeline.from_pretrained(
            "THUDM/CogVideoX-2b",
            torch_dtype=torch.float16,
        ).to("cuda")
        self.pipe.enable_model_cpu_offload()
        self.pipe.vae.enable_tiling()
        print("[CogVideoX] Model loaded ✓")

    @modal.method()
    def generate(self, prompt: str, duration_s: int = 6, seed: int = 42) -> bytes:
        import io, torch
        import imageio.v3 as iio

        n_frames = min(duration_s * 8, 49)  # CogVideoX max = 49 frames @ 8fps
        result   = self.pipe(
            prompt=prompt,
            num_videos_per_prompt=1,
            num_inference_steps=35,
            num_frames=n_frames,
            guidance_scale=6.0,
            generator=torch.Generator("cuda").manual_seed(seed),
        )
        frames = result.frames[0]  # list of PIL images

        buf = io.BytesIO()
        iio.imwrite(buf, frames, extension=".mp4",
                    codec="h264", fps=8, quality=8)
        buf.seek(0)
        return buf.read()


# ── Web endpoint (called by Railway worker via HTTP) ─────────────────────────
@app.function(image=image)
@modal.web_endpoint(method="POST")
async def generate_clip(item: dict) -> bytes:
    """
    POST body: {"prompt": "...", "duration_s": 6, "seed": 42}
    Returns: raw MP4 bytes
    """
    prompt     = item.get("prompt", "Cinematic lifestyle scene")
    duration_s = int(item.get("duration_s", 6))
    seed       = int(item.get("seed", 42))

    worker = CogVideoWorker()
    return worker.generate.remote(prompt, duration_s, seed)


# ── Local test entrypoint ─────────────────────────────────────────────────────
@app.local_entrypoint()
def test():
    result = CogVideoWorker().generate.remote(
        prompt=(
            "Pakistani woman in kitchen, morning light, ordering groceries on phone, "
            "cinematic warm tones, shallow depth of field, 9:16 vertical"
        ),
        duration_s=6,
        seed=42,
    )
    out = "/tmp/test_cogvideo.mp4"
    with open(out, "wb") as f:
        f.write(result)
    print(f"[Test] Saved to {out} ({len(result):,} bytes)")
