"""
ai_images.py — Free AI image generation via Pollinations.ai (no API key, no GPU)
Generates scene-specific images from text prompts, then animates with Ken Burns.
"""
import os, subprocess, urllib.parse
from pathlib import Path
from typing import Optional

import httpx

W, H = 1080, 1920

# Scene-type → cinematic image prompt prefix
SCENE_PROMPTS = {
    "hook":         "Cinematic close-up shot, Pakistani person frustrated expression, morning light, kitchen",
    "problem":      "Realistic photo, problem situation, stressed person, Pakistani home, warm tones",
    "product":      "Ultra-realistic product shot, grocery delivery app on phone, Pakistani hand, clean",
    "benefits":     "Warm cinematic photo, happy Pakistani family, fresh groceries, golden hour light",
    "social_proof": "Realistic photo, happy satisfied Pakistani customer, phone notification, smiling",
    "cta":          "Clean dark background, EstaMart logo, teal accent, modern Pakistani grocery brand",
}

STYLE_SUFFIX = (
    "vertical 9:16 format, cinematic photography, shallow depth of field, "
    "warm Pakistani home interior, professional DSLR, --ar 9:16"
)


def build_prompt(scene_type: str, keywords: list, visual_description: str = "") -> str:
    base    = SCENE_PROMPTS.get(scene_type, "Cinematic Pakistani lifestyle photo")
    detail  = visual_description or " ".join(keywords[:4])
    return f"{base}, {detail}, {STYLE_SUFFIX}"


async def fetch_ai_image(
    prompt: str,
    out_path: Path,
    width: int = W,
    height: int = H,
    seed: int = 42,
) -> Optional[Path]:
    """
    Fetch AI-generated image from Pollinations.ai (100% free, no key needed).
    Uses Flux model. Returns path or None on failure.
    """
    encoded = urllib.parse.quote(prompt)
    url     = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&seed={seed}&nologo=true&model=flux"
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            if r.headers.get("content-type", "").startswith("image"):
                out_path.write_bytes(r.content)
                return out_path
    except Exception:
        pass
    return None


def image_to_video(img_path: str, duration: float, out_path: str,
                   motion: str = "zoom_in") -> None:
    """
    Animate a static image with Ken Burns effect → MP4.
    On static images zoompan is fast (no decode overhead like real video).
    """
    duration = max(duration, 3.0)
    fps      = 25
    frames   = int(duration * fps)

    if motion == "zoom_in":
        zp = (f"scale=1350:2400,"
              f"zoompan=z='min(zoom+0.0004,1.2)':d={frames}"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}")
    elif motion == "zoom_out":
        zp = (f"scale=1350:2400,"
              f"zoompan=z='if(lte(on,1),1.2,max(zoom-0.0004,1.0))':d={frames}"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}")
    elif motion == "pan_right":
        zp = (f"scale=1350:2400,"
              f"zoompan=z=1.15:d={frames}"
              f":x='min(iw/2-(iw/zoom/2)+on*1.5,iw*(1-1/zoom))'"
              f":y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}")
    else:
        zp = (f"scale=1350:2400,"
              f"zoompan=z=1.15:d={frames}"
              f":x='max(iw*(1-1/zoom)-on*1.5,0)'"
              f":y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}")

    grade = ("eq=brightness=0.02:saturation=1.22:contrast=1.10"
             ":gamma=1.04:gamma_r=1.05:gamma_b=0.96,vignette=0.698")
    vf    = f"{zp},{grade}"

    subprocess.run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", img_path,
        "-vf", vf,
        "-t", str(duration),
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-an", out_path,
    ], check=True, capture_output=True)
