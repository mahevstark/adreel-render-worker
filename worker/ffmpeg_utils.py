"""
ffmpeg_utils.py — AdReel v5
Exact 60s formula, EBU R128, ASS karaoke, micro-shot engine for Mode B.

MICRO-SHOT MODE B:
  Each 10.333s scene = 8 micro-shots × 1.2917s each (no xfade overhead inside scene).
  Between scenes: 0.4s xfade → total = 6×10.333 + 5×(-0.4) = 60.0s exactly.

NEVER put `curves` with single-quoted spline values in same -vf as drawtext.
NEVER use nullsrc+geq as -i source with single-quoted params.
Use color=c=#hex as source. All animation in -vf only.
"""
import os, subprocess, tempfile
from pathlib import Path
from typing import List, Optional

W, H, FPS = 1080, 1920, 25

# ── Exact 60s timing ──────────────────────────────────────────────────────────
N_SCENES      = 6
XFADE_DUR     = 0.4          # seconds
SCENE_DUR     = (60.0 + (N_SCENES - 1) * XFADE_DUR) / N_SCENES  # 10.3333s
XFADE_STEP    = SCENE_DUR - XFADE_DUR                             # 9.9333s

# ── Mode B micro-shot timing ──────────────────────────────────────────────────
MICRO_PER_SCENE  = 8                          # micro-shots per scene
MICRO_DUR        = SCENE_DUR / MICRO_PER_SCENE  # 1.2917s each

# ── Ken Burns motions (cycle through for variety) ─────────────────────────────
MICRO_MOTIONS = [
    "zoom_in", "zoom_out", "pan_right", "pan_left",
    "zoom_in_tl", "zoom_in_br", "pan_up", "pan_down",
]

# ── Color grade (no curves, no PI) ────────────────────────────────────────────
GRADE_VF = (
    "eq=brightness=0.02:saturation=1.22:contrast=1.10"
    ":gamma=1.04:gamma_r=1.05:gamma_b=0.96,vignette=0.698"
)

# ── xfade transition styles ───────────────────────────────────────────────────
TRANSITION_STYLES = [
    "fade", "slideleft", "slideup", "smoothleft", "circleopen", "pixelize",
]

# ── Fast preset for motion clips ──────────────────────────────────────────────
FAST_MOTION = ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p"]


def _run(cmd: list, **kw) -> None:
    subprocess.run(cmd, check=True, capture_output=True, **kw)


def get_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def safe_text(text: str) -> str:
    import re
    t = re.sub(r"\*+|_+|`+|#+", "", str(text)).strip()
    return t.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:")


# ── Single micro-shot: static image → 1.29s MP4 with Ken Burns ───────────────
def make_micro_shot(img_path: str, out_path: str, motion: str = "zoom_in",
                    duration: float = MICRO_DUR) -> None:
    """
    Animate a static image into a short video with Ken Burns effect.
    Fast (CPU) because we're encoding a still image, not transcoding video.
    """
    dur    = max(duration, 0.5)
    frames = int(dur * FPS)

    # Each motion pattern: oversized canvas → zoompan crop to 1080×1920
    oversized = f"scale=1350:2400"
    cx        = "iw/2-(iw/zoom/2)"
    cy        = "ih/2-(ih/zoom/2)"

    if motion == "zoom_in":
        zp = f"zoompan=z='min(zoom+0.0008,1.22)':d={frames}:x='{cx}':y='{cy}':s={W}x{H}:fps={FPS}"
    elif motion == "zoom_out":
        zp = f"zoompan=z='if(lte(on,1),1.22,max(zoom-0.0008,1.0))':d={frames}:x='{cx}':y='{cy}':s={W}x{H}:fps={FPS}"
    elif motion == "pan_right":
        zp = (f"zoompan=z=1.18:d={frames}"
              f":x='min({cx}+on*2.5,iw*(1-1/zoom))':y='{cy}':s={W}x{H}:fps={FPS}")
    elif motion == "pan_left":
        zp = (f"zoompan=z=1.18:d={frames}"
              f":x='max({cx}-on*2.5,0)':y='{cy}':s={W}x{H}:fps={FPS}")
    elif motion == "pan_up":
        zp = (f"zoompan=z=1.18:d={frames}"
              f":x='{cx}':y='max({cy}-on*2.5,0)':s={W}x{H}:fps={FPS}")
    elif motion == "pan_down":
        zp = (f"zoompan=z=1.18:d={frames}"
              f":x='{cx}':y='min({cy}+on*2.5,ih*(1-1/zoom))':s={W}x{H}:fps={FPS}")
    elif motion == "zoom_in_tl":
        zp = (f"zoompan=z='min(zoom+0.0008,1.22)':d={frames}"
              f":x='max({cx}-on*1,0)':y='max({cy}-on*1,0)':s={W}x{H}:fps={FPS}")
    else:  # zoom_in_br
        zp = (f"zoompan=z='min(zoom+0.0008,1.22)':d={frames}"
              f":x='min({cx}+on*1,iw*(1-1/zoom))'"
              f":y='min({cy}+on*1,ih*(1-1/zoom))':s={W}x{H}:fps={FPS}")

    vf = f"{oversized},{zp},{GRADE_VF}"
    _run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", img_path,
        "-vf", vf,
        "-t", str(dur),
        "-r", str(FPS),
        *FAST_MOTION, "-an", out_path,
    ])


# ── Stitch N micro-shots into one scene clip (hard cuts = TikTok energy) ──────
def stitch_micro_shots(shot_paths: List[str], out_path: str) -> None:
    """
    Concatenate micro-shots with hard cuts using FFmpeg concat demuxer.
    Hard cuts at 1.29s intervals give TikTok/Reels kinetic energy.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, dir=os.path.dirname(out_path)) as f:
        for p in shot_paths:
            f.write(f"file '{p}'\n")
        list_path = f.name
    try:
        _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy",
            out_path,
        ])
    finally:
        os.unlink(list_path)


# ── Trim & grade a real video clip ────────────────────────────────────────────
def trim_and_grade(src: str, duration: float, out: str, motion_idx: int = 0) -> None:
    trans = TRANSITION_STYLES[motion_idx % len(TRANSITION_STYLES)]
    vf    = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},{GRADE_VF}"
    _run([
        "ffmpeg", "-y", "-i", src,
        "-vf", vf,
        "-t", str(duration),
        *FAST_MOTION, "-an", out,
    ])


# ── Fallback: plain color card ────────────────────────────────────────────────
def make_color_card(color: str, duration: float, out: str,
                    text: Optional[str] = None) -> None:
    vf_parts = []
    if text:
        safe = safe_text(text[:55])
        vf_parts.append(
            f"drawtext=text='{safe}':fontsize=72:fontcolor=white"
            f":bordercolor=black:borderw=5:x=(w-text_w)/2:y=(h-text_h)/2"
        )
    vf = ",".join(vf_parts) if vf_parts else "null"
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:size={W}x{H}:rate={FPS}",
        "-vf", vf,
        "-t", str(duration),
        *FAST_MOTION, out,
    ])


# ── xfade stitch → exactly 60.0s ─────────────────────────────────────────────
def compose_xfade(clips: List[str], out: str) -> None:
    assert len(clips) == N_SCENES, f"Need {N_SCENES} clips, got {len(clips)}"
    n = len(clips)
    inputs = []
    for c in clips:
        inputs += ["-i", c]

    fc_parts = []
    prev = "0:v"
    for i in range(1, n):
        offset = round(i * XFADE_STEP, 6)
        trans  = TRANSITION_STYLES[i % len(TRANSITION_STYLES)]
        tag    = f"v{i}"
        fc_parts.append(
            f"[{prev}][{i}:v]xfade=transition={trans}"
            f":duration={XFADE_DUR}:offset={offset}[{tag}]"
        )
        prev = tag

    _run([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(fc_parts),
        "-map", f"[{prev}]",
        "-t", "60.0",
        *FAST_MOTION, "-an", out,
    ])


# ── EBU R128 loudness normalization ──────────────────────────────────────────
def normalize_loudness(src: str, out: str) -> None:
    _run([
        "ffmpeg", "-y", "-i", src,
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100", "-ac", "2", out,
    ])


# ── Mix voice + optional music, pad to exact video duration ──────────────────
def mix_audio(video: str, voice: str, out: str,
              music_path: Optional[str] = None, music_vol: float = 0.10) -> None:
    vid_dur = get_duration(video)
    if music_path and os.path.exists(music_path):
        af = (
            f"[1:a]apad=whole_dur={vid_dur}[voice];"
            f"[2:a]aloop=loop=-1:size=2e+09,volume={music_vol},"
            f"atrim=0:{vid_dur}[music];"
            f"[voice][music]amix=inputs=2:duration=first[aout]"
        )
        _run([
            "ffmpeg", "-y",
            "-i", video, "-i", voice, "-i", music_path,
            "-filter_complex", af,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest",
            out,
        ])
    else:
        af = f"apad=whole_dur={vid_dur}"
        _run([
            "ffmpeg", "-y",
            "-i", video, "-i", voice,
            "-filter_complex", f"[1:a]{af}[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac",
            "-t", str(vid_dur), out,
        ])


# ── Burn ASS karaoke captions ────────────────────────────────────────────────
def burn_ass_captions(video: str, ass_path: str, out: str) -> None:
    # Forward-slash path, escape colon on Windows
    safe_ass = ass_path.replace("\\", "/").replace(":", "\\:")
    _run([
        "ffmpeg", "-y", "-i", video,
        "-vf", f"subtitles='{safe_ass}'",
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "copy", out,
    ])


# ── Fallback drawtext captions (word cards) ───────────────────────────────────
def build_word_captions(text: str, duration: float, n_words: int = 3) -> list:
    words   = text.split()
    groups  = [words[i:i+n_words] for i in range(0, len(words), n_words)]
    if not groups:
        return []
    step    = duration / len(groups)
    caps    = []
    for i, g in enumerate(groups):
        caps.append({"start": round(i * step, 3),
                     "end":   round((i + 1) * step, 3),
                     "text":  " ".join(g)})
    return caps


def burn_captions(video: str, captions: list, style: str, out: str) -> None:
    if not captions:
        import shutil
        shutil.copy(video, out)
        return
    vf_parts = []
    for cap in captions:
        t   = safe_text(cap["text"])
        s   = cap["start"]
        e   = cap["end"]
        a   = (f"if(lt(t,{s+0.12}),0,"
               f"if(lt(t,{s+0.24}),(t-{s+0.12})/0.12,"
               f"if(lt(t,{e-0.10}),1,(t-{e-0.10})/0.10*-1+1)))")
        vf_parts.append(
            f"drawtext=text='{t}':fontsize=72:fontcolor=white"
            f":bordercolor=black:borderw=5"
            f":x=(w-text_w)/2:y=h*0.82"
            f":enable='between(t,{s},{e})':alpha='{a}'"
        )
    _run([
        "ffmpeg", "-y", "-i", video,
        "-vf", ",".join(vf_parts),
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "copy", out,
    ])


# ── Thumbnail ─────────────────────────────────────────────────────────────────
def extract_thumbnail(video: str, out: str, t: float = 1.5) -> None:
    _run([
        "ffmpeg", "-y", "-ss", str(t), "-i", video,
        "-frames:v", "1", "-q:v", "2", out,
    ])
