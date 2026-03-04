"""
Scene template renderer — AdReel v3
Generates animated motion-graphic scenes via FFmpeg (no Pexels, no GPU).
Each scene type has a unique color palette + animated background + kinetic text.
"""
import subprocess
from pathlib import Path

# ── Brand palettes per scene type ────────────────────────────────────────────
SCENE_PALETTES = {
    "hook":         {"bg": "0x0a0014", "accent": "0xe040fb", "text": "white"},
    "problem":      {"bg": "0x1a0000", "accent": "0xff5252", "text": "white"},
    "product":      {"bg": "0x001428", "accent": "0x40c4ff", "text": "white"},
    "benefits":     {"bg": "0x00140a", "accent": "0x69f0ae", "text": "white"},
    "social_proof": {"bg": "0x0a0a00", "accent": "0xffd740", "text": "white"},
    "cta":          {"bg": "0x14000a", "accent": "0xff4081", "text": "white"},
}

W, H, FPS = 1080, 1920, 25

# Simple solid bg colors per scene (no geq — avoids single-quote conflicts)
SCENE_BG_COLORS = {
    "hook":         "#0d0020",
    "problem":      "#1a0000",
    "product":      "#00101e",
    "benefits":     "#001a0a",
    "social_proof": "#0e0e00",
    "cta":          "#1a0010",
}


def _safe(text: str) -> str:
    """Escape text for FFmpeg drawtext."""
    import re
    t = re.sub(r"\*+|_+|`+|#+", "", str(text)).strip()
    return t.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:")


def make_scene(
    tmp: Path,
    index: int,
    scene_type: str,
    duration: float,
    headline: str = "",
    subline: str = "",
) -> str:
    """Render an animated motion-graphic scene MP4 using color source + -vf filters."""
    out      = str(tmp / f"scene_{index}.mp4")
    duration = max(duration, 3.0)
    pal      = SCENE_PALETTES.get(scene_type, SCENE_PALETTES["product"])
    bg_color = SCENE_BG_COLORS.get(scene_type, "#0a0a14")

    # All filters go in -vf (NOT in -i source) to avoid single-quote conflicts
    vf_parts = []

    # Accent bar at 1/3 height (static — dynamic alpha breaks on some FFmpeg builds)
    vf_parts.append(
        f"drawbox=x=80:y=640:w=920:h=8:color={pal['accent']}:t=fill"
    )

    # Headline: fade-in only (no y-expression — simpler, more compatible)
    if headline:
        h_safe  = _safe(headline[:55])
        alpha_h = "if(lt(t,0.15),0,if(lt(t,0.5),(t-0.15)/0.35,1))"
        vf_parts.append(
            f"drawtext=text='{h_safe}':fontsize=80:fontcolor=white"
            f":bordercolor=black:borderw=5"
            f":x=(w-text_w)/2:y=720"
            f":alpha='{alpha_h}'"
        )

    # Subline: fade-in delayed
    if subline:
        s_safe  = _safe(subline[:70])
        alpha_s = "if(lt(t,0.4),0,if(lt(t,0.75),(t-0.4)/0.35,1))"
        vf_parts.append(
            f"drawtext=text='{s_safe}':fontsize=50:fontcolor=white@0.80"
            f":bordercolor=black:borderw=3"
            f":x=(w-text_w)/2:y=860"
            f":alpha='{alpha_s}'"
        )

    # Small accent dot bottom center
    vf_parts.append(
        f"drawbox=x=528:y=1780:w=24:h=24:color={pal['accent']}:t=fill"
    )

    # Grade overlay
    vf_parts.append(
        "eq=brightness=0.02:saturation=1.22:contrast=1.10:gamma=1.04:gamma_r=1.05:gamma_b=0.96"
    )

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c={bg_color}:size={W}x{H}:rate={FPS}",
        "-vf", ",".join(vf_parts),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        out,
    ], check=True, capture_output=True)

    return out
