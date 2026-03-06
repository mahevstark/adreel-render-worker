"""
modal_worker.py — Mode A: Wan2.1-T2V-14B on Modal.com serverless GPU
──────────────────────────────────────────────────────────────────────────────
MODEL: Wan-AI/Wan2.1-T2V-14B
  - Best open-source text-to-video model (2025)
  - Supports 9:16 vertical (1080×1920) natively
  - 480p and 720p output
  - Up to ~5s clips (81 frames @ 16fps)

⚠️  GPU REQUIREMENTS:
    Wan2.1-14B  → 24+ GB VRAM minimum
    Recommended: A100-40GB or H100

COST ESTIMATE (Modal.com):
    A100-40GB: ~$0.00180/GPU-second
    ~200s render per clip × 6 clips = ~1200 GPU-seconds
    Cost per 60s video ≈ ~$0.72 – $1.20
    Free credits: $30 = ~25–40 free videos

    Wan2.1-1.3B (faster option):
    A10G: ~$0.00059/GPU-second
    ~60s render per clip = ~360 GPU-seconds per video
    Cost per video ≈ ~$0.21
    Free credits: $30 = ~140 free videos

SETUP:
    1. pip install modal
    2. modal token new  (one-time auth)
    3. modal deploy worker/modal_worker.py
    4. Set MODAL_ENDPOINT=<your-modal-web-url> in Railway env vars
    5. Set RENDER_MODE=ai in Railway env vars

DEPLOY:
    cd adreel-worker
    modal deploy worker/modal_worker.py
"""
import modal

# ── App definition ────────────────────────────────────────────────────────────
app = modal.App("adreel-wan21")

# Base image with all required deps for Wan2.1
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "ffmpeg", "libgl1")
    .pip_install(
        "torch>=2.4.0",
        "torchvision",
        "torchaudio",
        "transformers>=4.49.0",
        "accelerate>=0.30.0",
        "diffusers>=0.32.0",
        "huggingface_hub>=0.23.0",
        "safetensors",
        "imageio[ffmpeg]>=2.34.0",
        "opencv-python-headless",
        "einops",
        "flash-attn",        # Required for Wan2.1 attention
        "fastapi[standard]>=0.115.0",
        "sentencepiece",
        "ftfy",
    )
)

# Persistent volume — model weights cached across cold starts (~28GB one-time download)
model_volume = modal.Volume.from_name("wan21-weights", create_if_missing=True)

# ── GPU worker class ──────────────────────────────────────────────────────────
@app.cls(
    gpu="A100-40GB",        # Wan2.1-14B needs 24GB+ VRAM; A100-40GB is safe
    image=image,
    volumes={"/root/model_cache": model_volume},
    timeout=900,            # 15 min max per request
    allow_concurrent_inputs=1,
    secrets=[modal.Secret.from_name("huggingface-secret")],  # optional HF token
)
class Wan21Worker:
    @modal.enter()
    def load(self):
        import os, torch
        from diffusers import AutoencoderKLWan, WanPipeline
        from diffusers.schedulers.scheduling_unipc_multistep import UniPCMultistepScheduler
        from transformers import AutoTokenizer, UMT5EncoderModel

        os.environ["HF_HOME"] = "/root/model_cache"
        os.environ["TRANSFORMERS_CACHE"] = "/root/model_cache"

        model_id = "Wan-AI/Wan2.1-T2V-14B"
        print(f"[Wan2.1] Loading {model_id} ...")

        self.pipe = WanPipeline.from_pretrained(
            model_id,
            torch_dtype=torch.bfloat16,
        )
        self.pipe.scheduler = UniPCMultistepScheduler.from_config(
            self.pipe.scheduler.config,
            flow_shift=5.0,   # Wan2.1 recommended
        )
        self.pipe.to("cuda")
        print("[Wan2.1] Model loaded on CUDA ✓")

    @modal.method()
    def generate(
        self,
        prompt: str,
        negative_prompt: str = "blurry, low quality, distorted, watermark, text, ugly",
        width: int = 832,
        height: int = 480,
        num_frames: int = 81,     # 81 frames @ 16fps ≈ 5.06s
        guidance_scale: float = 5.0,
        num_inference_steps: int = 30,
        seed: int = 42,
    ) -> bytes:
        """
        Generate a video clip and return raw MP4 bytes.
        Default: 832×480, 81 frames (~5s). For 9:16 vertical use 480×832.
        """
        import io, torch
        import imageio.v3 as iio

        generator = torch.Generator("cuda").manual_seed(seed)

        print(f"[Wan2.1] Generating: {prompt[:80]}...")
        output = self.pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_frames=num_frames,
            guidance_scale=guidance_scale,
            num_inference_steps=num_inference_steps,
            generator=generator,
        )

        frames = output.frames[0]   # list of PIL images

        # Encode to MP4 bytes
        buf = io.BytesIO()
        frame_arrays = [f for f in frames]  # PIL list
        iio.imwrite(
            buf,
            frame_arrays,
            extension=".mp4",
            codec="h264",
            fps=16,
            quality=9,
        )
        buf.seek(0)
        data = buf.read()
        print(f"[Wan2.1] Done — {len(data):,} bytes, {len(frames)} frames")
        return data


# ── Faster/cheaper variant: Wan2.1-1.3B on A10G ──────────────────────────────
@app.cls(
    gpu="A10G",
    image=image,
    volumes={"/root/model_cache": model_volume},
    timeout=600,
    allow_concurrent_inputs=1,
)
class Wan21FastWorker:
    """Wan2.1-T2V-1.3B — faster, cheaper, still better than CogVideoX-2B."""

    @modal.enter()
    def load(self):
        import os, torch
        from diffusers import WanPipeline
        from diffusers.schedulers.scheduling_unipc_multistep import UniPCMultistepScheduler

        os.environ["HF_HOME"] = "/root/model_cache"
        model_id = "Wan-AI/Wan2.1-T2V-1.3B"
        print(f"[Wan2.1-Fast] Loading {model_id} ...")
        self.pipe = WanPipeline.from_pretrained(
            model_id,
            torch_dtype=torch.float16,
        )
        self.pipe.scheduler = UniPCMultistepScheduler.from_config(
            self.pipe.scheduler.config,
            flow_shift=3.0,
        )
        self.pipe.to("cuda")
        print("[Wan2.1-Fast] Ready ✓")

    @modal.method()
    def generate(
        self,
        prompt: str,
        negative_prompt: str = "blurry, low quality, distorted, watermark",
        width: int = 832,
        height: int = 480,
        num_frames: int = 81,
        guidance_scale: float = 5.0,
        num_inference_steps: int = 25,
        seed: int = 42,
    ) -> bytes:
        import io, torch
        import imageio.v3 as iio

        generator = torch.Generator("cuda").manual_seed(seed)
        output = self.pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_frames=num_frames,
            guidance_scale=guidance_scale,
            num_inference_steps=num_inference_steps,
            generator=generator,
        )
        frames = output.frames[0]
        buf = io.BytesIO()
        iio.imwrite(buf, frames, extension=".mp4", codec="h264", fps=16, quality=8)
        buf.seek(0)
        return buf.read()


# ── Web endpoint — Railway calls this via HTTP POST ───────────────────────────
@app.function(image=image)
@modal.web_endpoint(method="POST")
async def generate_clip(item: dict) -> bytes:
    """
    POST body:
    {
      "prompt": "...",
      "negative_prompt": "...",   // optional
      "width": 832,               // optional, default 832
      "height": 480,              // optional, default 480 (use 480x832 for 9:16)
      "num_frames": 81,           // optional, max 81
      "guidance_scale": 5.0,      // optional
      "num_inference_steps": 30,  // optional
      "seed": 42,                 // optional
      "quality": "fast"           // optional: "fast" = 1.3B, else 14B
    }
    Returns: raw MP4 bytes (Content-Type: video/mp4)
    """
    prompt           = item.get("prompt", "Cinematic lifestyle delivery scene, Lahore Pakistan")
    negative_prompt  = item.get("negative_prompt", "blurry, low quality, distorted, watermark, text")
    width            = int(item.get("width", 480))   # 9:16 vertical default
    height           = int(item.get("height", 832))  # 9:16 vertical default
    num_frames       = int(item.get("num_frames", 81))
    guidance_scale   = float(item.get("guidance_scale", 5.0))
    steps            = int(item.get("num_inference_steps", 30))
    seed             = int(item.get("seed", 42))
    quality          = item.get("quality", "best")  # "fast" or "best"

    if quality == "fast":
        worker = Wan21FastWorker()
        video_bytes = worker.generate.remote(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_frames=num_frames,
            guidance_scale=guidance_scale,
            num_inference_steps=min(steps, 25),
            seed=seed,
        )
    else:
        worker = Wan21Worker()
        video_bytes = worker.generate.remote(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_frames=num_frames,
            guidance_scale=guidance_scale,
            num_inference_steps=steps,
            seed=seed,
        )

    return video_bytes


# ── Local test ─────────────────────────────────────────────────────────────────
@app.local_entrypoint()
def test():
    """Run: modal run worker/modal_worker.py"""
    print("[Test] Running Wan2.1-T2V-14B test...")
    result = Wan21Worker().generate.remote(
        prompt=(
            "Pakistani woman in modern Lahore kitchen, morning sunlight streaming through window, "
            "ordering groceries on smartphone, warm golden tones, cinematic bokeh, "
            "vertical 9:16 framing, professional food delivery commercial"
        ),
        negative_prompt="blurry, low quality, distorted, watermark, text, ugly faces",
        width=480,
        height=832,
        num_frames=81,
        num_inference_steps=30,
        seed=2026,
    )
    out = "/tmp/test_wan21.mp4"
    with open(out, "wb") as f:
        f.write(result)
    print(f"[Test] Saved: {out} ({len(result):,} bytes)")
    print("[Test] Download and check quality before setting MODAL_ENDPOINT in Railway!")
