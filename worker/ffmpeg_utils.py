"""
FFmpeg utility functions — AdReel v3
Cinematic grade, camera motion, xfade transitions, captions with fade.
"""
import os, re, shutil, subprocess
from pathlib import Path

# ── Cinematic LUT (applied to every clip for consistent look) ─────────────────
# Warm-film grade: slight lift in shadows, push blue/teal in highlights
GRADE_VF = (
    "eq=brightness=0.02:saturation=1.22:contrast=1.10:gamma=1.04,"
    "curves=r='0/0 0.5/0.47 1/0.93':g='0/0 0.5/0.50 1/1.00':b='0/0 0.5/0.53 1/1.06',"
    "vignette=PI/4.5"
)

TRANSITION_STYLES = ["fade", "slideleft", "slideup", "fadeblack", "wipeleft"]
MOTION_TYPES      = ["zoom_in", "zoom_out", "pan_right", "zoom_in", "zoom_out", "pan_left"]

# Zoompan is CPU-intensive — default OFF on Railway (set CINEMATIC_MOTION=1 to enable)
FAST_MOTION = os.environ.get("CINEMATIC_MOTION", "0") != "1"


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        return float(r.stdout.strip() or "0")
    except Exception:
        return 0.0


def safe_text(raw: str) -> str:
    """Strip markdown + escape chars for FFmpeg drawtext."""
    t = re.sub(r"\*+|_+|`+|#+", "", str(raw)).strip()
    return t.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")


# ── Per-clip: trim + scale + grade + camera motion ────────────────────────────
def trim_and_grade(src: str, duration: float, out: str, motion_idx: int = 0):
    duration = max(duration, 3.0)
    src_dur  = get_duration(src)
    if src_dur <= 0:
        src_dur = duration
    loops = max(1, int(duration / src_dur) + 1)

    fps    = 25
    frames = int(duration * fps)

    if FAST_MOTION:
        # Fast path: simple scale+crop, no per-frame calc
        motion_vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    else:
        m = MOTION_TYPES[motion_idx % len(MOTION_TYPES)]
        # Scale slightly larger than output for room to move (1.2×)
        if m == "zoom_in":
            motion_vf = (
                f"scale=1350:2400,"
                f"zoompan=z='min(zoom+0.0005,1.2)':d={frames}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps}"
            )
        elif m == "zoom_out":
            motion_vf = (
                f"scale=1350:2400,"
                f"zoompan=z='if(lte(on,1),1.2,max(zoom-0.0005,1.0))':d={frames}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps}"
            )
        elif m == "pan_right":
            motion_vf = (
                f"scale=1350:2400,"
                f"zoompan=z=1.2:d={frames}"
                f":x='min(iw/2-(iw/zoom/2)+on*2,iw*(1-1/zoom))'"
                f":y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps}"
            )
        else:  # pan_left
            motion_vf = (
                f"scale=1350:2400,"
                f"zoompan=z=1.2:d={frames}"
                f":x='max(iw*(1-1/zoom)-on*2,0)'"
                f":y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps}"
            )

    vf = f"{motion_vf},{GRADE_VF}"

    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", str(loops - 1),
        "-i", src,
        "-t", str(duration),
        "-vf", vf,
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-an", out,
    ], check=True, capture_output=True)


# ── Fallback: dark branded card + optional text ───────────────────────────────
def make_color_card(tmp: Path, index: int, duration: float,
                    overlay_text: list | None = None) -> str:
    out      = str(tmp / f"card_{index}.mp4")
    duration = max(duration, 3.0)
    fps      = 25

    vf_parts = [GRADE_VF]
    if overlay_text:
        for li, line in enumerate(overlay_text[:3]):
            s = safe_text(line)
            if not s:
                continue
            y = 780 + li * 120
            vf_parts.append(
                f"drawtext=text='{s}':fontsize=72:fontcolor=white"
                f":bordercolor=black:borderw=5:x=(w-text_w)/2:y={y}"
            )

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=#1a1a2e:size=1080x1920:rate={fps}",
        "-vf", ",".join(vf_parts),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        out,
    ], check=True, capture_output=True)
    return out


# ── xfade chain ───────────────────────────────────────────────────────────────
def compose_xfade(clips: list, out_path: str, xfade_dur: float = 0.4):
    n = len(clips)
    if n == 0:
        raise ValueError("No clips to compose")
    if n == 1:
        shutil.copy(clips[0], out_path)
        return

    scene_dur = get_duration(clips[0]) or 10.0
    args = ["ffmpeg", "-y"]
    for c in clips:
        args += ["-i", c]

    parts = []
    prev = "[0:v]"
    for i in range(1, n):
        curr   = f"[{i}:v]"
        offset = round(i * (scene_dur - xfade_dur), 3)
        label  = f"[x{i}]" if i < n - 1 else "[vout]"
        trans  = TRANSITION_STYLES[(i - 1) % len(TRANSITION_STYLES)]
        parts.append(
            f"{prev}{curr}xfade=transition={trans}"
            f":duration={xfade_dur}:offset={offset}{label}"
        )
        prev = f"[x{i}]"

    args += [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-an", out_path,
    ]
    subprocess.run(args, check=True, capture_output=True)


# ── Audio mix: loop video to audio length ────────────────────────────────────
def mix_audio_sync(video_path: str, audio_path: str, out_path: str):
    if not os.path.exists(audio_path):
        shutil.copy(video_path, out_path)
        return
    audio_dur = get_duration(audio_path)
    video_dur = get_duration(video_path)
    if audio_dur <= 0:
        shutil.copy(video_path, out_path)
        return
    loops = max(1, int(audio_dur / max(video_dur, 0.1)) + 1)
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", str(loops - 1), "-i", video_path,
        "-i", audio_path,
        "-t", str(audio_dur),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        out_path,
    ], check=True, capture_output=True)


# ── Captions with fade-in/out ─────────────────────────────────────────────────
def burn_captions(video_path: str, captions: list, style: str, out_path: str):
    if not captions:
        shutil.copy(video_path, out_path)
        return

    sz  = {"bold": 68, "punchy": 76, "minimal": 52}.get(style, 68)
    col = {"bold": "white", "punchy": "yellow", "minimal": "white"}.get(style, "white")
    bw  = {"bold": 5, "punchy": 4, "minimal": 2}.get(style, 4)

    vf_parts = []
    for cap in captions:
        s  = float(cap.get("start_s", 0))
        e  = float(cap.get("end_s", s + 2))
        t  = safe_text(cap.get("text", ""))
        if not t:
            continue
        fi = s + 0.3      # fade-in end
        fo = e - 0.25     # fade-out start
        alpha = (
            f"if(lt(t,{s:.3f}),0,"
            f"if(lt(t,{fi:.3f}),(t-{s:.3f})/0.3,"
            f"if(lt(t,{fo:.3f}),1,"
            f"if(lt(t,{e:.3f}),({e:.3f}-t)/0.25,0))))"
        )
        vf_parts.append(
            f"drawtext=text='{t}':fontsize={sz}:fontcolor={col}"
            f":bordercolor=black:borderw={bw}"
            f":x=(w-text_w)/2:y=h*0.82-text_h/2"
            f":enable='between(t,{s:.3f},{e:.3f})':alpha='{alpha}'"
        )

    if not vf_parts:
        shutil.copy(video_path, out_path)
        return

    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", ",".join(vf_parts),
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "copy",
        out_path,
    ], check=True, capture_output=True)


# ── Thumbnail ────────────────────────────────────────────────────────────────
def extract_thumbnail(video_path: str, out_path: str):
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-ss", "00:00:02", "-frames:v", "1", "-q:v", "2",
        out_path,
    ], check=True, capture_output=True)
