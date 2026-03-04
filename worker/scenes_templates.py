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
    """Render an animated motion-graphic scene MP4."""
    out = str(tmp / f"scene_{index}.mp4")
    duration = max(duration, 3.0)
    pal = SCENE_PALETTES.get(scene_type, SCENE_PALETTES["product"])
    frames = int(duration * FPS)

    # ── Animated gradient background using geq ────────────────────────────────
    # Each scene type gets unique wave params so backgrounds look distinct
    wave_params = {
        "hook":         ("sin(2*PI*X/1080+t*1.2)*40+80",  "10", "sin(2*PI*(X+Y)/800+t*0.8)*60+20"),
        "problem":      ("sin(2*PI*X/1080+t*0.6)*80+80",  "5",  "5"),
        "product":      ("5",  "sin(2*PI*Y/1920+t*0.9)*40+20", "sin(2*PI*X/600+t*1.1)*80+80"),
        "benefits":     ("5",  "sin(2*PI*(X+Y)/1000+t)*60+80", "20"),
        "social_proof": ("sin(2*PI*X/900+t*0.5)*40+60",  "sin(2*PI*Y/1200+t*0.4)*40+60", "5"),
        "cta":          ("sin(2*PI*X/700+t*1.4)*80+60",  "5",  "sin(2*PI*Y/800+t*1.0)*60+20"),
    }.get(scene_type, ("30", "30", "100"))
    r_expr, g_expr, b_expr = wave_params
    geq = f"geq=r='{r_expr}':g='{g_expr}':b='{b_expr}'"

    # ── Accent bar (horizontal rule) at 1/3 height ────────────────────────────
    accent_hex = pal["accent"].replace("0x", "")
    accent_rgb = tuple(int(accent_hex[i:i+2], 16) for i in (0, 2, 4))

    # ── Drawtext chain ────────────────────────────────────────────────────────
    vf_parts = [f"nullsrc=size={W}x{H}:rate={FPS}", geq, "format=yuv420p"]

    # Animated accent bar: fades in over first 0.5s
    vf_parts.append(
        f"drawbox=x=80:y=h/3-4:w=w-160:h=8:"
        f"color={pal['accent']}@'if(lt(t,0.5),t/0.5,1)':t=fill"
    )

    # Headline: big, bold, centered — slides up from y+30 to final position
    if headline:
        h_safe = _safe(headline[:60])
        # Slide-up: y starts at h*0.38+30, ends at h*0.38, over 0.4s
        y_expr = f"if(lt(t,0.4),h*0.38+30*(1-t/0.4),h*0.38)"
        alpha_h = f"if(lt(t,0.15),0,if(lt(t,0.45),(t-0.15)/0.3,1))"
        vf_parts.append(
            f"drawtext=text='{h_safe}':fontsize=84:fontcolor=white"
            f":bordercolor=black:borderw=5"
            f":x=(w-text_w)/2:y='{y_expr}'"
            f":alpha='{alpha_h}'"
        )

    # Subline: medium, below headline
    if subline:
        s_safe = _safe(subline[:80])
        alpha_s = f"if(lt(t,0.4),0,if(lt(t,0.7),(t-0.4)/0.3,1))"
        vf_parts.append(
            f"drawtext=text='{s_safe}':fontsize=52:fontcolor=white@0.85"
            f":bordercolor=black:borderw=3"
            f":x=(w-text_w)/2:y=h*0.50"
            f":alpha='{alpha_s}'"
        )

    # Pulsing accent dot (bottom center) — visual rhythm marker
    vf_parts.append(
        f"drawbox=x=(w-24)/2:y=h-120:w=24:h=24:"
        f"color={pal['accent']}@'0.6+0.4*sin(2*PI*t*1.5)':t=fill"
    )

    # Build command: nullsrc as -f lavfi -i, then -vf for geq+rest
    # Split: first vf_part is the source filter, rest are video filters
    src_filter   = ",".join(vf_parts[:3])   # nullsrc + geq + format
    video_filter = ",".join(vf_parts[3:])   # drawbox + drawtext etc

    if video_filter:
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", src_filter,
            "-vf", video_filter,
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            out,
        ], check=True, capture_output=True)
    else:
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", src_filter,
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            out,
        ], check=True, capture_output=True)

    return out
