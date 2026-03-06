"""
wan_client.py — Free Wan2.1 via HuggingFace Spaces Gradio API
──────────────────────────────────────────────────────────────────────────────
Uses the official Wan-AI/Wan2.1 HuggingFace Space as a FREE GPU backend.
No Modal account, no API key, no cost — Wan-AI pays for the GPU.

HOW IT WORKS:
  1. gradio_client connects to huggingface.co/spaces/Wan-AI/Wan2.1
  2. Submits a text-to-video job to their Gradio queue
  3. Waits for result (can take 30s–5min depending on queue)
  4. Downloads the MP4 bytes
  5. Returns them to pipeline.py exactly like Modal did

LIMITATIONS (free tier trade-offs):
  - Queue wait times: 30s–10min during peak hours
  - Rate limits: HF may throttle heavy usage
  - No SLA: Space could go down or be paused
  - Output resolution: 480p (Space default)
  - Max ~5s clips (81 frames)

SET IN RAILWAY ENV:
  RENDER_MODE=ai
  WAN_BACKEND=hf_space    ← uses this free HF Space client
  WAN_BACKEND=modal       ← uses paid Modal endpoint (better uptime)

If WAN_BACKEND is not set, defaults to hf_space (free).
"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

# HF token is optional — public spaces work without it
# Set HF_TOKEN env var for higher rate limits
HF_TOKEN = os.environ.get("HF_TOKEN", None)
HF_SPACE  = "Wan-AI/Wan2.1"


async def generate_wan_hf(
    prompt: str,
    negative_prompt: str = "blurry, low quality, distorted, watermark, text",
    width: int = 832,
    height: int = 480,
    num_frames: int = 81,
    guidance_scale: float = 5.0,
    num_inference_steps: int = 30,
    seed: int = 42,
    timeout: int = 600,
) -> Optional[bytes]:
    """
    Call the Wan-AI/Wan2.1 HuggingFace Space via gradio_client.
    Returns raw MP4 bytes or None on failure.

    Runs in a thread pool to avoid blocking the async event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _generate_sync,
        prompt, negative_prompt, width, height,
        num_frames, guidance_scale, num_inference_steps, seed, timeout,
    )


def _generate_sync(
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    num_frames: int,
    guidance_scale: float,
    num_inference_steps: int,
    seed: int,
    timeout: int,
) -> Optional[bytes]:
    """Synchronous Gradio client call (runs in thread pool)."""
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        print("[WanHF] gradio_client not installed. Run: pip install gradio_client")
        return None

    try:
        print(f"[WanHF] Connecting to {HF_SPACE} ...")
        client = Client(
            HF_SPACE,
            hf_token=HF_TOKEN,
        )

        print(f"[WanHF] Submitting job: {prompt[:60]}...")
        # The Wan2.1 Space exposes a /generate endpoint
        # Parameters match the Gradio interface
        result = client.predict(
            prompt=prompt,
            negative_prompt=negative_prompt,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            seed=seed,
            api_name="/generate",
        )

        # Result is a filepath to the generated video on HF servers
        if isinstance(result, str) and result.endswith(".mp4"):
            video_path = Path(result)
            if video_path.exists():
                data = video_path.read_bytes()
                print(f"[WanHF] Generated {len(data):,} bytes ✓")
                return data
        elif isinstance(result, dict) and "video" in result:
            video_path = Path(result["video"])
            if video_path.exists():
                return video_path.read_bytes()

        print(f"[WanHF] Unexpected result type: {type(result)} — {str(result)[:200]}")
        return None

    except Exception as e:
        print(f"[WanHF] Error: {e}")
        return None


async def generate_wan_modal(
    prompt: str,
    negative_prompt: str = "blurry, low quality, distorted, watermark, text",
    width: int = 480,
    height: int = 832,
    num_frames: int = 81,
    guidance_scale: float = 5.0,
    num_inference_steps: int = 30,
    seed: int = 42,
    quality: str = "best",
    modal_endpoint: str = "",
    timeout: int = 600,
) -> Optional[bytes]:
    """Call the deployed Modal Wan2.1 endpoint (paid, better uptime)."""
    import httpx
    if not modal_endpoint:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{modal_endpoint}/generate_clip",
                json={
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height,
                    "num_frames": num_frames,
                    "guidance_scale": guidance_scale,
                    "num_inference_steps": num_inference_steps,
                    "seed": seed,
                    "quality": quality,
                },
            )
            r.raise_for_status()
            return r.content
    except Exception as e:
        print(f"[WanModal] Error: {e}")
        return None


async def generate_wan(
    prompt: str,
    scene_idx: int = 0,
    duration: float = 5.0,
    modal_endpoint: str = "",
    out_path: Optional[Path] = None,
) -> Optional[Path]:
    """
    Unified entry point — tries HF Space first (free), falls back to Modal.
    Called from pipeline.py instead of the old generate_ai_scene().

    WAN_BACKEND env var:
      hf_space (default) — free HuggingFace Space
      modal              — paid Modal endpoint
    """
    backend = os.environ.get("WAN_BACKEND", "hf_space")
    seed    = 42 + scene_idx
    quality = os.environ.get("WAN_QUALITY", "best")

    negative = (
        "blurry, low quality, distorted faces, watermark, text overlay, "
        "overexposed, ugly, deformed, low resolution, duplicate objects"
    )

    video_bytes = None

    if backend == "modal" and modal_endpoint:
        print(f"[Wan] Backend: Modal ({quality})")
        video_bytes = await generate_wan_modal(
            prompt=prompt,
            negative_prompt=negative,
            width=480, height=832,  # 9:16 vertical
            num_frames=81,
            seed=seed,
            quality=quality,
            modal_endpoint=modal_endpoint,
        )
    else:
        print(f"[Wan] Backend: HF Space (free)")
        video_bytes = await generate_wan_hf(
            prompt=prompt,
            negative_prompt=negative,
            width=832, height=480,  # HF Space default (landscape)
            num_frames=81,
            seed=seed,
        )

    if not video_bytes:
        print(f"[Wan] Generation failed for scene {scene_idx}")
        return None

    if out_path is None:
        tmp = tempfile.mktemp(suffix=".mp4")
        out_path = Path(tmp)

    out_path.write_bytes(video_bytes)
    print(f"[Wan] Saved scene {scene_idx} → {out_path} ({len(video_bytes):,} bytes)")
    return out_path
