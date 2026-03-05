"""
ai_images.py — AdReel v6
Pollinations.ai (Flux, free, no GPU) with scene identity anchor.

SCENE IDENTITY ANCHOR:
  All 8 micro-shots in a scene share the same character/location/product.
  Only camera angle + lighting vary. This prevents "random world drift".

  Anchor = extracted from narration text + scene_type + brand context.
  Each shot: anchor + angle_variation + light_variation.
"""
import asyncio, re, urllib.parse
from pathlib import Path
from typing import Optional

import httpx

W, H = 1080, 1920

STYLE_SUFFIX = (
    "ultra-realistic DSLR photography, cinematic color grade, "
    "shallow depth of field, professional photography, "
    "Pakistani lifestyle setting, natural authentic light, 9:16 vertical"
)

NEG_PROMPT = (
    "cartoon, vector, illustration, 3D render, CGI, anime, "
    "low quality, blurry, overexposed, stock photo, watermark, "
    "western setting, fake smile, ring light, text, logo, multiple people unless specified"
)

# ── Scene-type → character + setting identity ─────────────────────────────────
# These are the ANCHORS — they stay consistent across all 8 shots of a scene.
SCENE_ANCHORS = {
    "hook":         "Pakistani woman 30s in lawn kameez, modern Lahore kitchen, morning",
    "problem":      "same Pakistani woman looking stressed, same Lahore kitchen, empty fridge",
    "product":      "same Pakistani woman holding phone showing EstaMart app, same kitchen counter",
    "benefits":     "same Pakistani woman smiling with fresh grocery bag, same bright kitchen",
    "social_proof": "same Pakistani woman at dining table, phone notification visible, satisfied",
    "cta":          "same Pakistani woman holding phone confidently, same modern kitchen, teal accent",
}

# ── Camera angle variations — vary per shot, keep subject same ────────────────
MICRO_ANGLES = [
    "extreme close-up on face showing emotion",
    "medium shot waist up, subject centered",
    "wide shot showing full kitchen environment",
    "over-shoulder angle from behind subject",
    "low angle 45 degrees looking up at subject",
    "close-up on hands and phone screen",
    "side profile silhouette against window light",
    "tight close-up on eyes and expression",
]

# ── Lighting variations — cinematic, not random ───────────────────────────────
MICRO_LIGHTS = [
    "warm golden morning light from window casting shadows",
    "soft diffused natural daylight, even exposure",
    "dramatic Rembrandt side lighting, deep shadows",
    "warm ambient kitchen light, intimate evening feel",
    "cool blue hour window light, moody atmosphere",
    "overhead warm key light, clean commercial look",
    "backlit silhouette, warm rim light from behind",
    "soft beauty dish key light, flattering skin tones",
]

# ── Narration keyword → scene anchor override ─────────────────────────────────
# If narration mentions these words, anchor shifts to match context
KEYWORD_ANCHOR_OVERRIDES = {
    "delivery": "EstaMart delivery rider in uniform on Lahore residential street, daytime",
    "rider":    "EstaMart delivery rider on motorcycle, Lahore residential street, golden hour",
    "fridge":   "Pakistani woman opening refrigerator in modern Lahore kitchen, morning light",
    "order":    "Pakistani woman tapping phone screen showing grocery app, kitchen counter",
    "fresh":    "close-up of fresh vegetables and fruits on kitchen counter, natural light",
    "family":   "Pakistani family of three at kitchen table, warm ambient light",
    "ramadan":  "Pakistani family at iftar table, dates and food visible, warm evening light",
    "sehri":    "Pakistani family at sehri table, pre-dawn 4AM soft light, intimate",
}


def extract_anchor(scene_type: str, narration: str, keywords: list) -> str:
    """
    Extract scene identity anchor from narration text.
    Checks keyword overrides first, falls back to scene_type default.
    This ensures all 8 shots stay in the same world.
    """
    text_lower = (narration + " " + " ".join(keywords)).lower()
    for kw, override in KEYWORD_ANCHOR_OVERRIDES.items():
        if kw in text_lower:
            return override
    return SCENE_ANCHORS.get(scene_type, "Pakistani person in modern Lahore home")


def build_micro_prompt(anchor: str, angle_idx: int, light_idx: int) -> str:
    """
    Build one micro-shot prompt from anchor + angle + light.
    The anchor is IDENTICAL across all 8 shots in a scene.
    """
    angle = MICRO_ANGLES[angle_idx % len(MICRO_ANGLES)]
    light = MICRO_LIGHTS[light_idx % len(MICRO_LIGHTS)]
    return f"{anchor}, {angle}, {light}, {STYLE_SUFFIX}"


# ── Keep backward-compatible build_prompt ────────────────────────────────────
def build_prompt(scene_type: str, keywords: list,
                 visual_description: str = "",
                 angle_idx: int = 0, light_idx: int = 0) -> str:
    anchor = extract_anchor(scene_type, visual_description, keywords)
    return build_micro_prompt(anchor, angle_idx, light_idx)


async def fetch_ai_image(
    prompt: str,
    out_path: Path,
    width: int = W,
    height: int = H,
    seed: int = 42,
    timeout: int = 60,
) -> Optional[Path]:
    """Fetch AI image from Pollinations.ai — FREE, Flux model, no API key."""
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
            if "image" in r.headers.get("content-type", ""):
                out_path.write_bytes(r.content)
                return out_path
    except Exception:
        pass
    return None


async def fetch_micro_shots(
    scene_type: str,
    keywords: list,
    visual_description: str,
    narration: str,
    tmp: Path,
    scene_idx: int,
    n_shots: int = 8,
) -> list[Optional[Path]]:
    """
    Fetch N varied AI images for one scene, all sharing the same anchor identity.
    Concurrent fetch — all N images requested in parallel.
    """
    anchor = extract_anchor(scene_type, narration or visual_description, keywords)

    async def fetch_one(shot_idx: int) -> Optional[Path]:
        prompt = build_micro_prompt(
            anchor,
            angle_idx=shot_idx,
            light_idx=(shot_idx + scene_idx * 3) % len(MICRO_LIGHTS),
        )
        out  = tmp / f"micro_{scene_idx}_{shot_idx}.jpg"
        seed = 1000 + scene_idx * 100 + shot_idx  # deterministic but varied
        return await fetch_ai_image(prompt, out, seed=seed, timeout=55)

    results = await asyncio.gather(
        *[fetch_one(i) for i in range(n_shots)], return_exceptions=False
    )
    return list(results)


def image_to_video(img_path: str, duration: float, out_path: str,
                   motion: str = "zoom_in") -> None:
    """Backward-compat wrapper → make_micro_shot."""
    from ffmpeg_utils import make_micro_shot
    make_micro_shot(img_path, out_path, motion=motion, duration=duration)
