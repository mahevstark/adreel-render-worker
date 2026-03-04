"""
AdReel render pipeline v3 — orchestration, scene planning, TTS, upload.
"""
import asyncio, hashlib, os, random, time, uuid
from pathlib import Path
from typing import Callable, Optional

import edge_tts
import httpx

from ffmpeg_utils import (
    get_duration, trim_and_grade, make_color_card,
    compose_xfade, mix_audio_sync, burn_captions,
    build_word_captions, extract_thumbnail,
)
from scenes_templates import make_scene, SCENE_PALETTES

# ── Semantic scene → keyword map ──────────────────────────────────────────────
SCENE_KWMAP: dict[str, list[str]] = {
    "hook":         ["close up face surprise reaction", "problem frustration struggle"],
    "problem":      ["stress overwhelmed difficulty searching", "tired exhausted person"],
    "product":      ["product unboxing reveal packaging", "delivery package door arrival"],
    "benefits":     ["happy satisfied family smile success", "comfortable convenient lifestyle"],
    "social_proof": ["customer happy satisfied review", "positive results achievement people"],
    "cta":          ["phone ordering online shopping app", "click button purchase delivery"],
}
SCENE_ORDER = ["hook", "problem", "product", "benefits", "social_proof", "cta"]

VOICE_MAP = {
    "professional_male":   "en-US-GuyNeural",
    "professional_female": "en-US-JennyNeural",
    "casual_male":         "en-US-ChristopherNeural",
    "casual_female":       "en-US-AriaNeural",
    "ur_male":             "ur-PK-AsadNeural",
    "ur_female":           "ur-PK-UzmaNeural",
}

PEXELS_KEY        = os.environ.get("PEXELS_API_KEY", "")
CLOUDINARY_CLOUD  = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_KEY    = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

_used_ids: set = set()   # dedup Pexels clip IDs per render

# ── Sentence-segmented TTS with natural pauses ───────────────────────────────
def segment_narration(text: str) -> str:
    """Insert natural pauses between sentences for edge-tts (SSML-style spacing)."""
    import re
    # Add double-space pause between sentences (edge-tts respects punctuation pauses)
    text = re.sub(r'([.!?])\s+', r'\1  ', text.strip())
    # Add slight pause after commas
    text = re.sub(r',\s+', r',  ', text)
    return text


# ── Plan normalizer: enforce 6 scenes × 10s ──────────────────────────────────
def normalize_plan(plan: dict) -> dict:
    scenes    = list(plan.get("scenes") or [])
    narration = list(plan.get("narration") or [])

    # Pad to 6 scenes if needed
    while len(scenes) < 6:
        idx   = len(scenes)
        stype = SCENE_ORDER[idx % len(SCENE_ORDER)]
        kws   = random.choice(SCENE_KWMAP[stype]).split()
        scenes.append({
            "id": f"s{idx+1}", "type": "broll",
            "scene_type": stype, "duration_s": 10.0,
            "search_keywords": kws, "overlay_text": [],
        })
    scenes = scenes[:6]

    # Inject scene_type + fix duration
    for i, s in enumerate(scenes):
        s["duration_s"] = 10.0
        if "scene_type" not in s:
            s["scene_type"] = SCENE_ORDER[i % len(SCENE_ORDER)]

    # Distribute narration text across scenes
    full_text  = " ".join(n.get("text", "") for n in narration)
    words      = full_text.split()
    words_per  = max(1, len(words) // 6)
    for i, s in enumerate(scenes):
        chunk    = words[i * words_per: (i + 1) * words_per]
        # fallback: use existing narration[i] text or chunk
        if i < len(narration) and narration[i].get("text"):
            text = narration[i]["text"]
        else:
            text = " ".join(chunk)
        s["_caption"] = text
        s["_cap_start"] = round(i * 9.6, 3)      # scene offset accounting for 0.4s xfade
        s["_cap_end"]   = round(i * 9.6 + 8.0, 3)

    plan["scenes"]     = scenes
    plan["duration_s"] = 60.0
    return plan


# ── TTS ───────────────────────────────────────────────────────────────────────
async def generate_tts(text: str, voice: str, out_path: str):
    await edge_tts.Communicate(text, voice).save(out_path)


# ── Pexels: fetch best-matching clip, avoid repeats ──────────────────────────
async def fetch_pexels(
    client: httpx.AsyncClient,
    keywords: list[str],
    duration_s: float,
    out: Path,
) -> Optional[Path]:
    if not PEXELS_KEY:
        return None
    query = " ".join(keywords[:3])
    try:
        r = await client.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "orientation": "portrait",
                    "size": "medium", "per_page": 10},
            headers={"Authorization": PEXELS_KEY},
        )
        r.raise_for_status()
        videos = [v for v in r.json().get("videos", []) if v["id"] not in _used_ids]
        if not videos:
            return None
        best = sorted(videos, key=lambda v: abs(v["duration"] - duration_s))[0]
        _used_ids.add(best["id"])
        vfile = next(
            (f for f in best["video_files"] if f.get("width", 9999) <= 720),
            best["video_files"][0],
        )
        dl = await client.get(vfile["link"])
        dl.raise_for_status()
        out.write_bytes(dl.content)
        return out
    except Exception:
        return None


# ── Cloudinary upload (streaming, memory-safe) ────────────────────────────────
async def upload_cloudinary(file_path: str, resource_type: str = "video") -> str:
    if not CLOUDINARY_CLOUD:
        return f"file://{file_path}"
    ts        = int(time.time())
    folder    = "adreel-renders"
    signature = hashlib.sha1(
        f"folder={folder}&timestamp={ts}{CLOUDINARY_SECRET}".encode()
    ).hexdigest()
    url       = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/{resource_type}/upload"
    fsize     = os.path.getsize(file_path)
    chunk_sz  = 20 * 1024 * 1024

    async with httpx.AsyncClient(timeout=300) as client:
        if fsize > 50 * 1024 * 1024:
            uid    = uuid.uuid4().hex
            pub_id = f"adreel/{uuid.uuid4().hex}"
            secure = None
            offset = 0
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_sz)
                    if not chunk:
                        break
                    end  = offset + len(chunk) - 1
                    resp = await client.post(
                        url,
                        data={"api_key": CLOUDINARY_KEY, "timestamp": str(ts),
                              "folder": folder, "signature": signature,
                              "public_id": pub_id},
                        files={"file": (os.path.basename(file_path), chunk)},
                        headers={"X-Unique-Upload-Id": uid,
                                 "Content-Range": f"bytes {offset}-{end}/{fsize}"},
                    )
                    if resp.status_code in (200, 201):
                        secure = resp.json().get("secure_url")
                    offset += len(chunk)
            return secure or f"file://{file_path}"
        else:
            with open(file_path, "rb") as f:
                data = f.read()
            resp = await client.post(
                url,
                data={"api_key": CLOUDINARY_KEY, "timestamp": str(ts),
                      "folder": folder, "signature": signature},
                files={"file": (os.path.basename(file_path), data)},
            )
            resp.raise_for_status()
            return resp.json()["secure_url"]


# ── Main render orchestrator ──────────────────────────────────────────────────
async def run_render(job_id: str, plan: dict, upd: Callable):
    import tempfile

    def u(status: str, pct: int, **kw):
        upd(job_id, status=status, progress=pct, **kw)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _used_ids.clear()
        try:
            # 0 — Normalise to 6 scenes × 10s
            plan   = normalize_plan(plan)
            scenes = plan["scenes"]

            # 1 — TTS: prefer full voiceover_script for long audio
            u("GENERATING_AUDIO", 5)
            # Priority: voiceover_script (full ad copy) > narration segments > captions
            narr_text = (
                plan.get("voiceover_script", "").strip()
                or " ".join(n.get("text", "") for n in plan.get("narration", [])).strip()
                or " ".join(s.get("_caption", "") for s in scenes).strip()
                or "Discover something amazing today."
            )

            voice     = VOICE_MAP.get(plan.get("voice_style", "professional_female"),
                                      "en-US-JennyNeural")
            audio_pth = tmp / "voice.mp3"
            narr_text_paced = segment_narration(narr_text)
            await asyncio.wait_for(
                generate_tts(narr_text_paced, voice, str(audio_pth)), timeout=90
            )

            # 2 — Build scene visuals: Pexels clip OR motion-graphic template
            u("FETCHING_ASSETS", 15)
            clips: list[str] = []

            # Extract per-scene headline from narration text
            narr_words   = narr_text.split()
            words_per_sc = max(1, len(narr_words) // 6)
            sc_headlines = [
                " ".join(narr_words[i * words_per_sc:(i + 1) * words_per_sc])[:50]
                for i in range(6)
            ]

            async with httpx.AsyncClient(timeout=30) as client:
                for i, scene in enumerate(scenes):
                    stype    = scene.get("scene_type", SCENE_ORDER[i % len(SCENE_ORDER)])
                    kws      = (
                        scene.get("search_keywords")
                        or random.choice(SCENE_KWMAP.get(stype, [["lifestyle"]])).split()
                    )
                    raw  = tmp / f"raw_{i}.mp4"
                    proc = str(tmp / f"proc_{i}.mp4")

                    # Try Pexels first
                    clip = await fetch_pexels(client, kws, 10.0, raw)
                    if clip and clip.exists():
                        try:
                            trim_and_grade(str(clip), 10.0, proc, motion_idx=i)
                            clips.append(proc)
                            u("FETCHING_ASSETS", 15 + i * 5)
                            continue
                        except Exception:
                            pass

                    # Fallback: motion-graphic template (better than color card)
                    headline = sc_headlines[i] if i < len(sc_headlines) else ""
                    subline  = stype.replace("_", " ").title()
                    mg_path  = make_scene(tmp, i, stype, 10.0, headline=headline, subline=subline)
                    clips.append(mg_path)
                    u("FETCHING_ASSETS", 15 + i * 5)

            # 3 — Compose with xfade transitions
            u("COMPOSITING", 50)
            composed = tmp / "composed.mp4"
            compose_xfade(clips, str(composed))

            # 4 — Mix audio (loop/trim video to match TTS)
            u("COMPOSITING", 68)
            with_audio = tmp / "with_audio.mp4"
            mix_audio_sync(str(composed), str(audio_pth), str(with_audio))

            # 5 — Word-by-word captions timed across full video
            u("COMPOSITING", 82)
            final    = tmp / "final.mp4"
            vid_dur  = get_duration(str(with_audio))
            captions = build_word_captions(narr_text, vid_dur)
            burn_captions(str(with_audio), captions, plan.get("caption_style", "bold"), str(final))

            # 6 — Upload
            u("UPLOADING", 90)
            video_url = await upload_cloudinary(str(final), "video")
            thumb     = tmp / "thumb.jpg"
            extract_thumbnail(str(final), str(thumb))
            thumb_url = await upload_cloudinary(str(thumb), "image")

            dur  = get_duration(str(final))
            size = os.path.getsize(str(final))
            u("DONE", 100, video_url=video_url, thumbnail_url=thumb_url,
              duration_s=round(dur, 1), file_size_bytes=size)

        except asyncio.TimeoutError:
            u("FAILED", 0, error="TTS timed out after 90s")
        except Exception as e:
            u("FAILED", 0, error=str(e))
            raise
