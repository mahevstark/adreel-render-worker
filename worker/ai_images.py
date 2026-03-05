"""
ai_images.py — AdReel v5
Free AI image generation via Pollinations.ai (Flux model, no API key, no GPU).
Supports micro-shot mode: generates N varied images per scene for TikTok energy.
"""
import asyncio, urllib.parse
from pathlib import Path
from typing import Optional

import httpx

W, H = 1080, 1920

STYLE_SUFFIX = (
    "ultra-realistic DSLR photography, cinematic color grade, "
    "shallow depth of field, professional photography, "
    "Pakistani lifestyle setting, natural light, 9:16 vertical frame"
)

NEG_PROMPT = (
    "cartoon, vector, illustration, 3D render, CGI, anime, "
    "low quality, blurry, overexposed, stock photo, watermark, "
    "western setting, fake smile, ring light"
)

# ── Per scene-type base prompt ────────────────────────────────────────────────
SCENE_BASE = {
    "hook":         "Pakistani person with frustrated surprised expression, morning kitchen",
    "problem":      "Stressed Pakistani person searching struggling, home interior",
    "product":      "Pakistani grocery delivery app on phone, fresh groceries unboxing",
    "benefits":     "Happy Pakistani family smiling together, fresh groceries, golden hour",
    "social_proof": "Satisfied Pakistani customer holding phone showing positive result",
    "cta":          "Pakistani person on phone ordering app, confident modern urban setting",
}

# ── Camera angle variations for micro-shots ───────────────────────────────────
MICRO_ANGLES = [
    "extreme close-up face",
    "medium shot torso and face",
    "wide environmental shot",
    "over-shoulder angle",
    "low angle looking up",
    "close-up hands and phone",
    "silhouette against window light",
    "top-down flat-lay",
]

# ── Lighting variations ───────────────────────────────────────────────────────
MICRO_LIGHTS = [
    "warm morning golden hour window light",
    "soft diffused natural daylight",
    "dramatic side lighting",
    "warm evening ambient light",
    "cool blue hour dusk light",
    "warm overhead kitchen light",
    "candlelit warm tones",
    "bright natural noon light",
]


def build_prompt(scene_type: str, keywords: list,
                 visual_description: str = "",
                 angle_idx: int = 0, light_idx: int = 0) -> str:
    """Build a cinematic image prompt with angle/lighting variation for micro-shots."""
    base   = SCENE_BASE.get(scene_type, "Pakistani lifestyle cinematic photo")
    detail = visual_description or " ".join(k for k in keywords[:3])
    angle  = MICRO_ANGLES[angle_idx % len(MICRO_ANGLES)]
    light  = MICRO_LIGHTS[light_idx % len(MICRO_LIGHTS)]
    return f"{base}, {detail}, {angle}, {light}, {STYLE_SUFFIX}"


async def fetch_ai_image(
    prompt: str,
    out_path: Path,
    width: int = W,
    height: int = H,
    seed: int = 42,
    timeout: int = 60,
) -> Optional[Path]:
    """
    Fetch AI image from Pollinations.ai — FREE, no API key, Flux model.
    Returns path on success, None on failure.
    """
    encoded = urllib.parse.quote(prompt)
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&seed={seed}"
        f"&nologo=true&model=flux&negative={urllib.parse.quote(NEG_PROMPT)}"
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "image" in ct:
                out_path.write_bytes(r.content)
                return out_path
    except Exception:
        pass
    return None


async def fetch_micro_shots(
    scene_type: str,
    keywords: list,
    visual_description: str,
    tmp: Path,
    scene_idx: int,
    n_shots: int = 8,
) -> list[Optional[Path]]:
    """
    Fetch N varied AI images for one scene (used in Mode B micro-shot engine).
    Runs fetches concurrently. Returns list of paths (None on failure).
    """
    async def fetch_one(shot_idx: int) -> Optional[Path]:
        prompt = build_prompt(
            scene_type, keywords, visual_description,
            angle_idx=shot_idx,
            light_idx=(shot_idx + scene_idx) % len(MICRO_LIGHTS),
        )
        out = tmp / f"micro_{scene_idx}_{shot_idx}.jpg"
        seed = 100 * scene_idx + shot_idx
        return await fetch_ai_image(prompt, out, seed=seed, timeout=50)

    results = await asyncio.gather(*[fetch_one(i) for i in range(n_shots)],
                                   return_exceptions=False)
    return list(results)


def image_to_video(img_path: str, duration: float, out_path: str,
                   motion: str = "zoom_in") -> None:
    """
    Animate a static image → MP4 with Ken Burns effect (uses make_micro_shot).
    Kept for backward compatibility.
    """
    from ffmpeg_utils import make_micro_shot
    make_micro_shot(img_path, out_path, motion=motion, duration=duration)
