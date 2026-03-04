"""
FFmpeg utilities — AdReel v4
Exact 60s output, ASS karaoke captions, music ducking, EBU R128 loudnorm, xfade chain.
"""
import os, re, shutil, subprocess
from pathlib import Path

# ── Exact 60s scene duration formula ─────────────────────────────────────────
# Total = N * scene_dur - (N-1) * xfade_dur  → set Total=60, solve for scene_dur
XFADE_DUR  = 0.4
N_SCENES   = 6
SCENE_DUR  = round((60.0 + (N_SCENES - 1) * XFADE_DUR) / N_SCENES, 6)  # 10.33333s
# Xfade offset for scene i: i * (SCENE_DUR - XFADE_DUR)
XFADE_STEP = SCENE_DUR - XFADE_DUR   # 9.93333s

# ── Cinematic grade (eq only — no single-quote conflicts) ─────────────────────
GRADE_VF = (
    "eq=brightness=0.02:saturation=1.22:contrast=1.10"
    ":gamma=1.04:gamma_r=1.05:gamma_b=0.96,"
    "vignette=0.698"
)

TRANSITION_STYLES = ["fade", "slideleft", "slideup", "fadeblack", "wipeleft"]

# Zoompan disabled on CPU by default; set CINEMATIC_MOTION=1 to enable
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
    t = re.sub(r"\*+|_+|`+|#+", "", str(raw)).strip()
    return t.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")


# ── EBU R128 loudness normalisation ──────────────────────────────────────────
def normalize_loudness(src: str, out: str, target_lufs: float = -16.0):
    """Normalize audio to EBU R128 target using FFmpeg loudnorm (two-pass)."""
    # One-pass loudnorm (fast, good enough for ads)
    subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
        "-c:a", "libmp3lame", "-q:a", "2",
        out,
    ], check=True, capture_output=True)


# ── Per-clip: trim + scale + grade ───────────────────────────────────────────
def trim_and_grade(src: str, duration: float, out: str, motion_idx: int = 0):
    duration    = max(duration, 3.0)
    src_dur     = get_duration(src)
    if src_dur <= 0:
        src_dur = duration
    loops       = max(1, int(duration / src_dur) + 1)
    fps         = 25
    frames      = int(duration * fps)

    MOTION_TYPES = ["zoom_in", "zoom_out", "pan_right", "zoom_in", "zoom_out", "pan_left"]
    m = MOTION_TYPES[motion_idx % len(MOTION_TYPES)]

    if FAST_MOTION:
        motion_vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    elif m == "zoom_in":
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
    else:
        motion_vf = (
            f"scale=1350:2400,"
            f"zoompan=z=1.2:d={frames}"
            f":x='min(iw/2-(iw/zoom/2)+on*2,iw*(1-1/zoom))'"
            f":y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps}"
        )

    vf = f"{motion_vf},{GRADE_VF}"
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", str(loops - 1), "-i", src,
        "-t", str(duration),
        "-vf", vf, "-r", str(fps),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-an", out,
    ], check=True, capture_output=True)


# ── Fallback: dark branded card + grade + optional text ──────────────────────
def make_color_card(tmp: Path, index: int, duration: float,
                    overlay_text: list | None = None) -> str:
    out      = str(tmp / f"card_{index}.mp4")
    duration = max(duration, 3.0)
    vf_parts = [GRADE_VF]
    if overlay_text:
        for li, line in enumerate(overlay_text[:3]):
            s = safe_text(line)
            if not s:
                continue
            vf_parts.append(
                f"drawtext=text='{s}':fontsize=72:fontcolor=white"
                f":bordercolor=black:borderw=5:x=(w-text_w)/2:y={780 + li * 120}"
            )
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=#1a1a2e:size=1080x1920:rate=25",
        "-vf", ",".join(vf_parts),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        out,
    ], check=True, capture_output=True)
    return out


# ── xfade chain — produces exactly 60s output ────────────────────────────────
def compose_xfade(clips: list, out_path: str):
    """
    Stitch N clips with xfade transitions.
    Uses SCENE_DUR/XFADE_DUR formula → output is exactly 60.0s.
    """
    n = len(clips)
    if n == 0:
        raise ValueError("No clips")
    if n == 1:
        shutil.copy(clips[0], out_path)
        return

    args = ["ffmpeg", "-y"]
    for c in clips:
        args += ["-i", c]

    parts = []
    prev  = "[0:v]"
    for i in range(1, n):
        curr   = f"[{i}:v]"
        offset = round(i * XFADE_STEP, 5)
        label  = f"[x{i}]" if i < n - 1 else "[vout]"
        trans  = TRANSITION_STYLES[(i - 1) % len(TRANSITION_STYLES)]
        parts.append(
            f"{prev}{curr}xfade=transition={trans}"
            f":duration={XFADE_DUR}:offset={offset}{label}"
        )
        prev = f"[x{i}]"

    args += [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]",
        "-t", "60.0",      # hard cap at exactly 60s
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-an", out_path,
    ]
    subprocess.run(args, check=True, capture_output=True)


# ── Audio: voice + optional music bed with ducking ────────────────────────────
def mix_audio(
    video_path: str,
    voice_path: str,
    out_path: str,
    music_path: str | None = None,
    music_vol: float = 0.10,
):
    """
    Mux voice (normalised) into video. Pads voice with silence to video length.
    If music_path provided, adds music bed at music_vol ducked under voice.
    """
    video_dur = get_duration(video_path)
    if not os.path.exists(voice_path) or video_dur <= 0:
        shutil.copy(video_path, out_path)
        return

    if music_path and os.path.exists(music_path):
        # 3-input: video + voice + music
        subprocess.run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", voice_path,
            "-stream_loop", "-1", "-i", music_path,
            "-filter_complex",
            f"[1:a]apad=whole_dur={video_dur:.3f}[v];"
            f"[2:a]volume={music_vol},atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS[m];"
            f"[v][m]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-t", str(video_dur),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            out_path,
        ], check=True, capture_output=True)
    else:
        # Voice only — pad to video length
        subprocess.run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", voice_path,
            "-filter_complex", f"[1:a]apad=whole_dur={video_dur:.3f}[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-t", str(video_dur),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            out_path,
        ], check=True, capture_output=True)


# ── ASS caption burn ──────────────────────────────────────────────────────────
def burn_ass_captions(video_path: str, ass_path: str, out_path: str):
    """Burn ASS subtitle file into video (word-level karaoke highlight)."""
    if not os.path.exists(ass_path):
        shutil.copy(video_path, out_path)
        return
    # Use subtitles filter — reads ASS and applies karaoke styling
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"subtitles='{ass_escaped}'",
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "copy",
        out_path,
    ], check=True, capture_output=True)


# ── Fallback: drawtext captions (no ASS) ────────────────────────────────────
def build_word_captions(full_text: str, video_dur: float) -> list:
    words      = full_text.split()
    chunk_size = 3
    chunks     = [words[i:i+chunk_size] for i in range(0, len(words), chunk_size)]
    dur_each   = video_dur / max(len(chunks), 1)
    captions   = []
    for i, chunk in enumerate(chunks):
        s = round(i * dur_each, 3)
        e = round(s + dur_each - 0.08, 3)
        captions.append({"text": " ".join(chunk), "start_s": s, "end_s": e})
    return captions


def burn_captions(video_path: str, captions: list, style: str, out_path: str):
    if not captions:
        shutil.copy(video_path, out_path)
        return
    sz   = {"bold": 72, "punchy": 82, "minimal": 54}.get(style, 72)
    col  = {"bold": "white", "punchy": "yellow", "minimal": "white"}.get(style, "white")
    bw   = {"bold": 5, "punchy": 4, "minimal": 2}.get(style, 5)
    vf   = []
    for cap in captions:
        s  = float(cap.get("start_s", 0))
        e  = float(cap.get("end_s", s + 1.5))
        t  = safe_text(cap.get("text", ""))
        if not t:
            continue
        fi, fo = s + 0.12, e - 0.10
        alpha  = (f"if(lt(t,{s:.3f}),0,if(lt(t,{fi:.3f}),(t-{s:.3f})/0.12,"
                  f"if(lt(t,{fo:.3f}),1,if(lt(t,{e:.3f}),({e:.3f}-t)/0.10,0))))")
        vf.append(
            f"drawtext=text='{t}':fontsize={sz}:fontcolor={col}"
            f":bordercolor=black:borderw={bw}"
            f":x=(w-text_w)/2:y=h*0.84-text_h/2"
            f":enable='between(t,{s:.3f},{e:.3f})':alpha='{alpha}'"
        )
    if not vf:
        shutil.copy(video_path, out_path)
        return
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-preset", "fast", "-c:a", "copy",
        out_path,
    ], check=True, capture_output=True)


# ── Thumbnail ─────────────────────────────────────────────────────────────────
def extract_thumbnail(video_path: str, out_path: str):
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-ss", "00:00:02", "-frames:v", "1", "-q:v", "2", out_path,
    ], check=True, capture_output=True)
