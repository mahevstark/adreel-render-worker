"""
captions.py — Word-level caption generation + ASS karaoke subtitle file
Uses faster-whisper for accurate word timestamps, falls back to TTS-rate estimation.
"""
import re
from pathlib import Path

try:
    from faster_whisper import WhisperModel
    _WHISPER_AVAILABLE = True
except ImportError:
    _WHISPER_AVAILABLE = False

# ── ASS file template (karaoke word-highlight style) ─────────────────────────
# PrimaryColour  = white  (base/upcoming words)
# SecondaryColour = yellow (currently spoken word fill)
# BorderStyle 1 = outline+shadow (not box)
ASS_HEADER = """\
[Script Info]
Title: AdReel Viral Captions
ScriptType: v4.00+
WrapStyle: 2
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,78,&H00FFFFFF,&H0000FFFF,&H00000000,&HAA000000,-1,0,0,0,100,100,2,0,1,5,3,2,40,40,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# Words-per-line for caption grouping
WORDS_PER_LINE = 4


# ── Timestamp formatter ───────────────────────────────────────────────────────
def _ts(seconds: float) -> str:
    """Convert seconds → ASS timestamp h:mm:ss.cc"""
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    cs = int(round((seconds % 1) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ── Word sanitiser ────────────────────────────────────────────────────────────
def _clean(word: str) -> str:
    w = re.sub(r"[{}\\]", "", word).strip()
    w = w.replace(":", "\\:").replace("&", "\\&")
    return w


# ── Build ASS from word list ──────────────────────────────────────────────────
def build_ass(
    words: list[tuple[str, float, float]],
    out_path: str,
    words_per_line: int = WORDS_PER_LINE,
) -> None:
    """
    words = [(word_str, start_s, end_s), ...]
    Generates ASS file with \\kf karaoke timing per word.
    """
    lines: list[str] = []
    for i in range(0, len(words), words_per_line):
        chunk = words[i : i + words_per_line]
        if not chunk:
            continue
        line_start = chunk[0][1]
        line_end   = chunk[-1][2] + 0.05   # tiny buffer
        parts      = []
        for word, ws, we in chunk:
            cs   = max(1, int(round((we - ws) * 100)))
            text = _clean(word)
            if text:
                parts.append(f"{{\\kf{cs}}}{text} ")
        text_str = "".join(parts).rstrip()
        lines.append(
            f"Dialogue: 0,{_ts(line_start)},{_ts(line_end)},"
            f"Default,,0,0,0,,{text_str}"
        )
    Path(out_path).write_text(ASS_HEADER + "\n".join(lines), encoding="utf-8")


# ── Whisper transcription → word list ────────────────────────────────────────
def transcribe_words(audio_path: str) -> list[tuple[str, float, float]]:
    """
    Runs faster-whisper (tiny model, CPU, int8) to get word-level timestamps.
    Returns [(word, start_s, end_s), ...] or [] on failure.
    """
    if not _WHISPER_AVAILABLE:
        return []
    try:
        model    = WhisperModel("tiny", device="cpu", compute_type="int8")
        segs, _  = model.transcribe(audio_path, word_timestamps=True,
                                    vad_filter=True)
        result   = []
        for seg in segs:
            if seg.words:
                for w in seg.words:
                    word = (w.word or "").strip()
                    if word:
                        result.append((word, float(w.start), float(w.end)))
        return result
    except Exception:
        return []


# ── Fallback: estimate timing from speech rate ────────────────────────────────
def estimate_words(full_text: str, audio_dur: float) -> list[tuple[str, float, float]]:
    """
    Distribute words evenly across audio duration (150 WPM estimate).
    Used when Whisper is unavailable or fails.
    """
    words = full_text.split()
    if not words or audio_dur <= 0:
        return []
    dur_per = audio_dur / len(words)
    return [
        (w, round(i * dur_per, 3), round((i + 1) * dur_per, 3))
        for i, w in enumerate(words)
    ]


# ── Main entry point ──────────────────────────────────────────────────────────
def generate_ass(
    audio_path: str,
    full_text: str,
    video_dur: float,
    out_path: str,
    use_whisper: bool = True,
) -> str:
    """
    Generate ASS karaoke caption file.
    Tries Whisper first; falls back to timing estimation.
    Returns out_path.
    """
    words: list[tuple[str, float, float]] = []
    if use_whisper and _WHISPER_AVAILABLE:
        words = transcribe_words(audio_path)
    if not words:
        words = estimate_words(full_text, video_dur)
    build_ass(words, out_path)
    return out_path
