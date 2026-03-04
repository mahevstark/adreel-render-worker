"""
AdReel render pipeline v4
RENDER_MODE: stock (Pexels) | motion (templates, default) | ai (Modal GPU)
Features: Whisper captions, EBU R128, sentence-paced TTS, exact 60s, music ducking.
"""
import asyncio, hashlib, os, random, re, time, uuid
from pathlib import Path
from typing import Callable, Optional

import edge_tts
import httpx

from ffmpeg_utils import (
    get_duration, trim_and_grade, make_color_card,
    compose_xfade, mix_audio, burn_ass_captions, burn_captions,
    build_word_captions, normalize_loudness, extract_thumbnail,
    SCENE_DUR, N_SCENES,
)
from scenes_templates import make_scene
from captions import generate_ass, _WHISPER_AVAILABLE
from ai_images import fetch_ai_image, image_to_video, build_prompt as build_img_prompt

# ── Config ────────────────────────────────────────────────────────────────────
RENDER_MODE  = os.environ.get("RENDER_MODE", "motion")   # stock|motion|ai
PEXELS_KEY   = os.environ.get("PEXELS_API_KEY", "")
MUSIC_URL    = os.environ.get("BACKGROUND_MUSIC_URL", "")  # optional royalty-free URL
MODAL_EP     = os.environ.get("MODAL_ENDPOINT", "")        # Mode A GPU endpoint

CLOUDINARY_CLOUD  = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_KEY    = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

VOICE_MAP = {
    "professional_male":   "en-US-GuyNeural",
    "professional_female": "en-US-JennyNeural",
    "casual_male":         "en-US-ChristopherNeural",
    "casual_female":       "en-US-AriaNeural",
    "ur_male":             "ur-PK-AsadNeural",
    "ur_female":           "ur-PK-UzmaNeural",
}

SCENE_KWMAP = {
    "hook":         ["close up face surprise reaction", "problem frustration struggle"],
    "problem":      ["stress overwhelmed difficulty searching", "tired exhausted person"],
    "product":      ["product unboxing reveal packaging", "delivery package door arrival"],
    "benefits":     ["happy satisfied family smile success", "comfortable convenient lifestyle"],
    "social_proof": ["customer happy satisfied review", "positive results achievement people"],
    "cta":          ["phone ordering online shopping app", "click button purchase delivery"],
}
SCENE_ORDER = ["hook", "problem", "product", "benefits", "social_proof", "cta"]
_used_ids: set = set()


# ── SSML-style pacing for edge-tts ───────────────────────────────────────────
def pace_narration(text: str) -> str:
    """Insert natural pauses for better edge-tts pacing."""
    # Sentence boundaries → longer pause (3 spaces for edge-tts)
    t = re.sub(r"([.!?])\s+", r"\1   ", text.strip())
    # Comma pauses
    t = re.sub(r",\s+", r",  ", t)
    # Remove multiple spaces from source but keep our intentional ones
    t = re.sub(r"[ \t]{4,}", "   ", t)
    return t


# ── Plan normaliser → exactly 6 scenes × SCENE_DUR ───────────────────────────
def normalize_plan(plan: dict) -> dict:
    scenes    = list(plan.get("scenes") or [])
    narration = list(plan.get("narration") or [])

    while len(scenes) < N_SCENES:
        idx   = len(scenes)
        stype = SCENE_ORDER[idx % N_SCENES]
        scenes.append({
            "id": f"s{idx+1}", "type": "broll", "scene_type": stype,
            "duration_s": SCENE_DUR, "search_keywords": [],
            "overlay_text": [],
        })
    scenes = scenes[:N_SCENES]

    for i, s in enumerate(scenes):
        s["duration_s"] = SCENE_DUR
        if "scene_type" not in s:
            s["scene_type"] = SCENE_ORDER[i % N_SCENES]

    # Distribute narration text across scenes
    full_text  = " ".join(n.get("text", "") for n in narration)
    words      = full_text.split()
    words_per  = max(1, len(words) // N_SCENES)
    for i, s in enumerate(scenes):
        chunk = words[i * words_per: (i + 1) * words_per]
        if i < len(narration) and narration[i].get("text"):
            s["_caption"] = narration[i]["text"]
        else:
            s["_caption"] = " ".join(chunk)

    plan["scenes"]     = scenes
    plan["duration_s"] = 60.0
    return plan


# ── TTS ───────────────────────────────────────────────────────────────────────
async def generate_tts(text: str, voice: str, out_path: str):
    await edge_tts.Communicate(text, voice).save(out_path)


# ── Pexels clip fetch ─────────────────────────────────────────────────────────
async def fetch_pexels(
    client: httpx.AsyncClient, keywords: list, duration_s: float, out: Path
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
        best  = sorted(videos, key=lambda v: abs(v["duration"] - duration_s))[0]
        _used_ids.add(best["id"])
        vfile = next((f for f in best["video_files"] if f.get("width", 9999) <= 720),
                     best["video_files"][0])
        dl = await client.get(vfile["link"])
        dl.raise_for_status()
        out.write_bytes(dl.content)
        return out
    except Exception:
        return None


# ── Modal AI scene generation (Mode A) ───────────────────────────────────────
async def generate_ai_scene(
    client: httpx.AsyncClient, prompt: str, scene_idx: int, duration: float, out: Path
) -> Optional[Path]:
    if not MODAL_EP:
        return None
    try:
        r = await client.post(
            f"{MODAL_EP}/generate",
            json={"prompt": prompt, "duration_s": min(duration, 10),
                  "seed": 42 + scene_idx},
            timeout=300,
        )
        r.raise_for_status()
        out.write_bytes(r.content)
        return out
    except Exception:
        return None


# ── Background music download ─────────────────────────────────────────────────
async def fetch_music(tmp: Path) -> Optional[str]:
    if not MUSIC_URL:
        return None
    music_out = tmp / "music.mp3"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(MUSIC_URL)
            r.raise_for_status()
            music_out.write_bytes(r.content)
            return str(music_out)
    except Exception:
        return None


# ── Cloudinary upload ─────────────────────────────────────────────────────────
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
            uid, pub_id, secure, offset = uuid.uuid4().hex, f"adreel/{uuid.uuid4().hex}", None, 0
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_sz)
                    if not chunk:
                        break
                    end  = offset + len(chunk) - 1
                    resp = await client.post(url,
                        data={"api_key": CLOUDINARY_KEY, "timestamp": str(ts),
                              "folder": folder, "signature": signature, "public_id": pub_id},
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
            resp = await client.post(url,
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
        mode = plan.get("render_mode", RENDER_MODE)

        try:
            # 0 — Normalize plan
            plan   = normalize_plan(plan)
            scenes = plan["scenes"]

            # 1 — TTS with sentence pacing
            u("GENERATING_AUDIO", 5)
            narr_text = (
                plan.get("voiceover_script", "").strip()
                or " ".join(n.get("text", "") for n in plan.get("narration", [])).strip()
                or " ".join(s.get("_caption", "") for s in scenes).strip()
                or "Discover something amazing today."
            )
            paced_text = pace_narration(narr_text)
            voice      = VOICE_MAP.get(plan.get("voice_style", "professional_female"),
                                       "en-US-JennyNeural")
            raw_audio  = tmp / "voice_raw.mp3"
            norm_audio = tmp / "voice.mp3"
            await asyncio.wait_for(
                generate_tts(paced_text, voice, str(raw_audio)), timeout=90
            )
            # EBU R128 loudness normalisation
            try:
                normalize_loudness(str(raw_audio), str(norm_audio))
            except Exception:
                import shutil as _sh
                _sh.copy(str(raw_audio), str(norm_audio))

            # 2 — Build scene visuals
            u("FETCHING_ASSETS", 15)
            clips: list[str] = []
            words      = narr_text.split()
            wps        = max(1, len(words) // N_SCENES)
            headlines  = [" ".join(words[i*wps:(i+1)*wps])[:55] for i in range(N_SCENES)]

            async with httpx.AsyncClient(timeout=30) as client:
                for i, scene in enumerate(scenes):
                    stype = scene.get("scene_type", SCENE_ORDER[i % N_SCENES])
                    kws   = (
                        scene.get("search_keywords")
                        or random.choice(SCENE_KWMAP.get(stype, [["lifestyle"]])).split()
                    )
                    raw  = tmp / f"raw_{i}.mp4"
                    proc = str(tmp / f"proc_{i}.mp4")

                    # Mode A: AI generation via Modal
                    if mode == "ai" and MODAL_EP:
                        prompt = scene.get("visual_description", " ".join(kws))
                        clip   = await generate_ai_scene(client, prompt, i, SCENE_DUR, raw)
                        if clip and clip.exists():
                            trim_and_grade(str(clip), SCENE_DUR, proc, motion_idx=i)
                            clips.append(proc)
                            u("FETCHING_ASSETS", 15 + i * 5)
                            continue

                    # Try Pexels stock footage first
                    if PEXELS_KEY:
                        clip = await fetch_pexels(client, kws, SCENE_DUR, raw)
                        if clip and clip.exists():
                            try:
                                trim_and_grade(str(clip), SCENE_DUR, proc, motion_idx=i)
                                clips.append(proc)
                                u("FETCHING_ASSETS", 15 + i * 5)
                                continue
                            except Exception:
                                pass

                    # Fallback 1: AI-generated image via Pollinations.ai (free, no GPU)
                    img_prompt  = build_img_prompt(
                        stype, kws,
                        scene.get("visual_description", ""),
                    )
                    img_path    = tmp / f"ai_img_{i}.jpg"
                    MOTIONS     = ["zoom_in", "zoom_out", "pan_right", "zoom_in", "zoom_out", "pan_left"]
                    ai_img      = await fetch_ai_image(img_prompt, img_path, seed=42 + i)
                    if ai_img and ai_img.exists():
                        try:
                            image_to_video(str(ai_img), SCENE_DUR, proc,
                                           motion=MOTIONS[i % len(MOTIONS)])
                            clips.append(proc)
                            u("FETCHING_ASSETS", 15 + i * 5)
                            continue
                        except Exception:
                            pass

                    # Fallback 2: motion-graphic template
                    mg = make_scene(
                        tmp, i, stype, SCENE_DUR,
                        headline=headlines[i] if i < len(headlines) else "",
                        subline=stype.replace("_", " ").title(),
                    )
                    clips.append(mg)
                    u("FETCHING_ASSETS", 15 + i * 5)

            # 3 — Compose with xfade → exactly 60s
            u("COMPOSITING", 50)
            composed = tmp / "composed.mp4"
            compose_xfade(clips, str(composed))

            # 4 — Music bed (optional)
            u("COMPOSITING", 60)
            music_path = await fetch_music(tmp)

            # 5 — Mix audio (voice + music, pad to 60s)
            u("COMPOSITING", 65)
            with_audio = tmp / "with_audio.mp4"
            mix_audio(str(composed), str(norm_audio), str(with_audio),
                      music_path=music_path, music_vol=0.10)

            # 6 — Generate ASS karaoke captions
            u("COMPOSITING", 78)
            vid_dur  = get_duration(str(with_audio))
            ass_path = str(tmp / "captions.ass")
            try:
                generate_ass(
                    str(norm_audio), narr_text, vid_dur, ass_path,
                    use_whisper=_WHISPER_AVAILABLE,
                )
            except Exception:
                from captions import estimate_words, build_ass
                build_ass(estimate_words(narr_text, vid_dur), ass_path)

            # 7 — Burn captions
            u("COMPOSITING", 85)
            final = tmp / "final.mp4"
            try:
                burn_ass_captions(str(with_audio), ass_path, str(final))
            except Exception:
                # ASS burn failed → fallback to drawtext
                captions = build_word_captions(narr_text, vid_dur)
                burn_captions(str(with_audio), captions,
                              plan.get("caption_style", "bold"), str(final))

            # 8 — Upload
            u("UPLOADING", 92)
            video_url = await upload_cloudinary(str(final), "video")
            thumb     = tmp / "thumb.jpg"
            extract_thumbnail(str(final), str(thumb))
            thumb_url = await upload_cloudinary(str(thumb), "image")

            dur  = get_duration(str(final))
            size = os.path.getsize(str(final))
            u("DONE", 100, video_url=video_url, thumbnail_url=thumb_url,
              duration_s=round(dur, 1), file_size_bytes=size,
              render_mode=mode, whisper_used=_WHISPER_AVAILABLE)

        except asyncio.TimeoutError:
            u("FAILED", 0, error="TTS timed out")
        except Exception as e:
            u("FAILED", 0, error=str(e))
            raise
