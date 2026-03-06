"""
wan_client.py — Free Wan2.1 via HuggingFace Spaces Gradio API
──────────────────────────────────────────────────────────────────────────────
Calls the official Wan-AI/Wan2.1 HuggingFace Space as a FREE GPU backend.
No Modal, no API key, no cost — Wan-AI runs the GPU.

SET IN RAILWAY:
  RENDER_MODE=ai           ← activates Mode A
  WAN_BACKEND=hf_space     ← default, free via HF Space
  WAN_BACKEND=modal        ← paid Modal endpoint
  HF_TOKEN=hf_xxx          ← optional, increases rate limits
"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

HF_TOKEN = os.environ.get("HF_TOKEN", None)
HF_SPACE  = "Wan-AI/Wan2.1"


# ── HuggingFace Space (free) ──────────────────────────────────────────────────
async def generate_wan_hf(
    prompt: str,
    negative_prompt: str = "blurry, low quality, distorted, watermark, text",
    num_inference_steps: int = 20,
    guidance_scale: float = 5.0,
    seed: int = 42,
    timeout: int = 600,
) -> Optional[bytes]:
    """Call Wan-AI/Wan2.1 HF Space via gradio_client. Returns MP4 bytes or None."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _generate_sync,
        prompt, negative_prompt, num_inference_steps, guidance_scale, seed, timeout,
    )


def _generate_sync(
    prompt: str,
    negative_prompt: str,
    num_inference_steps: int,
    guidance_scale: float,
    seed: int,
    timeout: int,
) -> Optional[bytes]:
    """Synchronous Gradio client call — runs in executor thread."""
    try:
        from gradio_client import Client
    except ImportError:
        print("[WanHF] ERROR: gradio_client not installed. pip install gradio_client")
        return None

    try:
        print(f"[WanHF] Connecting to HF Space: {HF_SPACE} ...")
        client = Client(HF_SPACE, hf_token=HF_TOKEN)

        # Discover available endpoints — log them so we can debug
        try:
            api_info = client.view_api(return_format="dict")
            endpoints = list(api_info.get("named_endpoints", {}).keys())
            print(f"[WanHF] Available endpoints: {endpoints}")
        except Exception as e:
            print(f"[WanHF] Could not inspect API: {e}")
            endpoints = []

        # Try known endpoint names in priority order
        # Wan2.1 Space T2V tab is typically the first or named /generate
        candidate_endpoints = [
            "/generate",
            "/generate_video",
            "/t2v",
            "/text_to_video",
            "/predict",
            "/run",
        ]
        # Prepend any discovered endpoints
        for ep in endpoints:
            if ep not in candidate_endpoints:
                candidate_endpoints.insert(0, ep)

        result = None
        last_error = None

        for ep in candidate_endpoints:
            try:
                print(f"[WanHF] Trying endpoint: {ep} ...")
                # Minimal call — just prompt + seed, let Space use its defaults
                result = client.predict(
                    prompt,
                    api_name=ep,
                )
                print(f"[WanHF] Endpoint {ep} succeeded → result type: {type(result)}")
                break
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "not found" in err_str.lower() or "invalid" in err_str.lower():
                    print(f"[WanHF] {ep} not found, trying next...")
                    continue
                else:
                    # Real error (auth, timeout etc) — log and stop
                    print(f"[WanHF] {ep} error: {e}")
                    break

        if result is None:
            print(f"[WanHF] All endpoints failed. Last error: {last_error}")
            return None

        # Extract video file path from result
        video_path = _extract_video_path(result)
        if video_path and video_path.exists():
            data = video_path.read_bytes()
            print(f"[WanHF] Video ready: {len(data):,} bytes ✓")
            return data

        print(f"[WanHF] No video file found in result: {repr(result)[:300]}")
        return None

    except Exception as e:
        print(f"[WanHF] Fatal error: {e}")
        return None


def _extract_video_path(result) -> Optional[Path]:
    """Extract video Path from various result shapes Gradio can return."""
    # String path
    if isinstance(result, str):
        p = Path(result)
        if p.exists() and p.suffix in (".mp4", ".webm", ".mov"):
            return p

    # Tuple/list — video usually first or last element
    if isinstance(result, (tuple, list)):
        for item in result:
            p = _extract_video_path(item)
            if p:
                return p

    # Dict with 'video' or 'path' key
    if isinstance(result, dict):
        for key in ("video", "path", "output", "file", "url"):
            if key in result:
                p = _extract_video_path(result[key])
                if p:
                    return p

    return None


# ── Modal endpoint (paid, better uptime) ─────────────────────────────────────
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
    """Call the deployed Modal Wan2.1 endpoint."""
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


# ── Unified entry point ───────────────────────────────────────────────────────
async def generate_wan(
    prompt: str,
    scene_idx: int = 0,
    duration: float = 5.0,
    modal_endpoint: str = "",
    out_path: Optional[Path] = None,
) -> Optional[Path]:
    """
    Route to correct backend based on WAN_BACKEND env var.
    WAN_BACKEND=hf_space (default) → free via Wan-AI HF Space
    WAN_BACKEND=modal              → paid Modal endpoint
    """
    backend = os.environ.get("WAN_BACKEND", "hf_space")
    seed    = 42 + scene_idx

    negative = (
        "blurry, low quality, distorted faces, watermark, text overlay, "
        "overexposed, ugly, deformed, low resolution, duplicate objects"
    )

    video_bytes: Optional[bytes] = None

    if backend == "modal" and modal_endpoint:
        print(f"[Wan] Using Modal backend (quality={os.environ.get('WAN_QUALITY','best')})")
        video_bytes = await generate_wan_modal(
            prompt=prompt,
            negative_prompt=negative,
            width=480, height=832,
            num_frames=81,
            seed=seed,
            quality=os.environ.get("WAN_QUALITY", "best"),
            modal_endpoint=modal_endpoint,
        )
    else:
        print(f"[Wan] Using FREE HF Space backend (Wan-AI/Wan2.1)")
        video_bytes = await generate_wan_hf(
            prompt=prompt,
            negative_prompt=negative,
            num_inference_steps=20,
            guidance_scale=5.0,
            seed=seed,
        )

    if not video_bytes:
        print(f"[Wan] Generation failed — scene {scene_idx} will use Mode B fallback")
        return None

    if out_path is None:
        out_path = Path(tempfile.mktemp(suffix=".mp4"))

    out_path.write_bytes(video_bytes)
    print(f"[Wan] Scene {scene_idx} saved → {out_path} ({len(video_bytes):,} bytes)")
    return out_path
