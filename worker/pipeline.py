"""
AdReel render pipeline v5
─────────────────────────────────────────────────────────────────────────────
RENDER_MODE env var:
  motion  (default) — Mode B: 8 AI micro-shots per scene, Ken Burns, hard cuts
  stock              — Mode B with Pexels clips (one per scene)
  ai                 — Mode A: GPU text-to-video via Modal CogVideoX endpoint

⚠️  Railway CPU CANNOT run CogVideoX/Wan2.1/LTX-Video.
    Those models require 8–24 GB VRAM. Mode A needs MODAL_ENDPOINT set.
    Mode B on CPU is the free, shippable path.

60s formula: SCENE_DUR = (60 + 5×0.4) / 6 = 10.3333s
             per micro-shot = 10.3333 / 8 = 1.2917s
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
    make_micro_shot, stitch_micro_shots,
    SCENE_DUR, N_SCENES, MICRO_PER_SCENE, MICRO_DUR, MICRO_MOTIONS, _detect_beat_offsets,
)
from scenes_templates import make_scene
from captions import generate_ass, _WHISPER_AVAILABLE
from ai_images import (
    fetch_ai_image, fetch_micro_shots, image_to_video,
    build_prompt as build_img_prompt, extract_anchor,
)

# ── Config ────────────────────────────────────────────────────────────────────
RENDER_MODE  = os.environ.get("RENDER_MODE", "motion")   # motion|stock|ai
PEXELS_KEY   = os.environ.get("PEXELS_API_KEY", "")
MUSIC_URL    = os.environ.get("BACKGROUND_MUSIC_URL", "")
MODAL_EP     = os.environ.get("MODAL_ENDPOINT", "")       # Mode A — requires GPU

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
    "hook":         ["close up face surprise reaction frustrated",
                     "person panic problem searching"],
    "problem":      ["stress overwhelmed difficulty empty searching",
                     "tired exhausted struggle inconvenience"],
    "product":      ["product delivery package fresh groceries arrival",
                     "phone app order confirmation unboxing"],
    "benefits":     ["happy satisfied family smile success lifestyle",
                     "comfortable convenient easy home relief"],
    "social_proof": ["customer happy satisfied testimonial review",
                     "positive results achievement people smiling"],
    "cta":          ["phone ordering online shopping app tap button",
                     "call to action download install click purchase"],
}
SCENE_ORDER = ["hook", "problem", "product", "benefits", "social_proof", "cta"]
_used_ids: set = set()


# ── SSML-style pacing ────────────────────────────────────────────────────────
def pace_narration(text: str) -> str:
    t = re.sub(r"([.!?])\s+", r"\1   ", text.strip())
    t = re.sub(r",\s+", r",  ", t)
    t = re.sub(r"[ \t]{4,}", "   ", t)
    return t


# ── Plan normaliser → exactly 6 scenes ───────────────────────────────────────
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

    full_text  = " ".join(n.get("text", "") for n in narration)
    words      = full_text.split()
    words_per  = max(1, len(words) // N_SCENES)
    for i, s in enumerate(scenes):
        s["duration_s"] = SCENE_DUR
        if "scene_type" not in s:
            s["scene_type"] = SCENE_ORDER[i % N_SCENES]
        if i < len(narration) and narration[i].get("text"):
            s["_caption"] = narration[i]["text"]
        else:
            chunk = words[i * words_per: (i + 1) * words_per]
            s["_caption"] = " ".join(chunk)

    plan["scenes"]     = scenes
    plan["duration_s"] = 60.0
    return plan


# ── TTS ───────────────────────────────────────────────────────────────────────
async def generate_tts(text: str, voice: str, out_path: str) -> None:
    await edge_tts.Communicate(text, voice).save(out_path)


# ── Narration-aware shot durations ────────────────────────────────────────────
def split_narration_to_shots(scene_narration: str, n_shots: int,
                              scene_dur: float) -> list[float]:
    """
    Split scene narration into n_shots phrase chunks and derive shot durations
    proportional to phrase length (longer phrase = longer shot).
    Falls back to equal timing if narration is empty.
    """
    if not scene_narration.strip():
        return [scene_dur / n_shots] * n_shots

    # Split on phrase boundaries: . ! ? , — and ...
    raw    = re.split(r'[.!?,;—]|\.\.\.',  scene_narration)
    chunks = [c.strip() for c in raw if c.strip()]

    # Pad / trim to exactly n_shots
    while len(chunks) < n_shots:
        # Duplicate last phrase to fill
        chunks.append(chunks[-1] if chunks else "")
    chunks = chunks[:n_shots]

    # Weight by word count → proportional duration
    word_counts = [max(1, len(c.split())) for c in chunks]
    total_words = sum(word_counts)
    durations   = [round(scene_dur * (wc / total_words), 3) for wc in word_counts]

    # Fix rounding error — add remainder to last shot
    diff = round(scene_dur - sum(durations), 3)
    durations[-1] = round(durations[-1] + diff, 3)
    return durations


# ── Mode B: build one scene from N AI micro-shots ─────────────────────────────
async def build_micro_scene(
    scene_idx: int,
    scene: dict,
    tmp: Path,
    headlines: list,
    scene_narration: str = "",
    bpm: float = 0.0,
) -> str:
    """
    Generate MICRO_PER_SCENE (8) AI images for a scene.
    All 8 shots share the same scene identity anchor (character/location/product).
    Shot durations are narration-aware (phrase-proportional).
    Stitched with hard cuts + whoosh SFX at each cut point.
    """
    stype   = scene.get("scene_type", SCENE_ORDER[scene_idx % N_SCENES])
    kws_raw = scene.get("search_keywords") or random.choice(
        SCENE_KWMAP.get(stype, [["lifestyle"]])
    ).split()
    visual  = scene.get("visual_description", "")
    narr    = scene_narration or scene.get("_caption", "")

    # Narration-aware shot durations
    shot_durs = split_narration_to_shots(narr, MICRO_PER_SCENE, SCENE_DUR)

    # Fetch all N micro-shot images concurrently (shared anchor identity)
    imgs = await fetch_micro_shots(
        scene_type=stype,
        keywords=kws_raw,
        visual_description=visual,
        narration=narr,
        tmp=tmp,
        scene_idx=scene_idx,
        n_shots=MICRO_PER_SCENE,
    )

    shot_paths: list[str] = []
    stype_colors = {
        "hook": "#0d0020", "problem": "#1a0000", "product": "#00101e",
        "benefits": "#001a0a", "social_proof": "#0e0e00", "cta": "#1a0010",
    }

    for shot_idx, img in enumerate(imgs):
        shot_out = str(tmp / f"shot_{scene_idx}_{shot_idx}.mp4")
        motion   = MICRO_MOTIONS[shot_idx % len(MICRO_MOTIONS)]
        dur      = shot_durs[shot_idx]

        if img and img.exists():
            try:
                make_micro_shot(str(img), shot_out, motion=motion, duration=dur)
                shot_paths.append(shot_out)
                continue
            except Exception:
                pass

        # Fallback: branded color card
        clr = stype_colors.get(stype, "#0a0a14")
        make_color_card(clr, dur, shot_out,
                        text=headlines[scene_idx] if shot_idx == 0 else None)
        shot_paths.append(shot_out)

    # Stitch with whoosh SFX at cut points + optional beat snap
    scene_out = str(tmp / f"scene_{scene_idx}.mp4")
    stitch_micro_shots(
        shot_paths, scene_out,
        add_sfx=True,
        bpm=bpm,
        durations=shot_durs,
    )
    return scene_out


# ── Mode A: Modal CogVideoX clip ─────────────────────────────────────────────
async def generate_ai_scene(
    client: httpx.AsyncClient, prompt: str, scene_idx: int, duration: float, out: Path
) -> Optional[Path]:
    """
    Call Modal CogVideoX endpoint for true AI-generated video clip.
    Requires MODAL_ENDPOINT env var. Railway CPU CANNOT run this locally.
    VRAM required: CogVideoX-2B = 14GB, CogVideoX1.5-5B = 24GB.
    """
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
        return out if out.exists() else None
    except Exception as e:
        print(f"[Mode A] Modal call failed scene {scene_idx}: {e}")
        return None


# ── Pexels ────────────────────────────────────────────────────────────────────
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


# ── Background music ──────────────────────────────────────────────────────────
async def fetch_music(tmp: Path) -> Optional[str]:
    if not MUSIC_URL:
        return None
    out = tmp / "music.mp3"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(MUSIC_URL)
            r.raise_for_status()
            out.write_bytes(r.content)
            return str(out)
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
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/{resource_type}/upload"
    fsize    = os.path.getsize(file_path)
    chunk_sz = 20 * 1024 * 1024
    async with httpx.AsyncClient(timeout=300) as client:
        if fsize > 50 * 1024 * 1024:
            uid = uuid.uuid4().hex
            pub_id = f"adreel/{uuid.uuid4().hex}"
            secure = None
            offset = 0
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_sz)
                    if not chunk:
                        break
                    end  = offset + len(chunk) - 1
                    resp = await client.post(url,
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
            resp = await client.post(url,
                data={"api_key": CLOUDINARY_KEY, "timestamp": str(ts),
                      "folder": folder, "signature": signature},
                files={"file": (os.path.basename(file_path), data)},
            )
            resp.raise_for_status()
            return resp.json()["secure_url"]


# ── Main render orchestrator ──────────────────────────────────────────────────
async def run_render(job_id: str, plan: dict, upd: Callable) -> None:
    import tempfile

    def u(status: str, pct: int, **kw):
        upd(job_id, status=status, progress=pct, **kw)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp  = Path(tmp_str)
        mode = plan.get("render_mode", RENDER_MODE)
        _used_ids.clear()

        try:
            # Step 0 — Normalize plan to exactly 6 scenes × SCENE_DUR
            plan   = normalize_plan(plan)
            scenes = plan["scenes"]

            # Step 1 — TTS with sentence pacing + EBU R128
            u("GENERATING_AUDIO", 5)
            narr_text = (
                plan.get("voiceover_script", "").strip()
                or " ".join(n.get("text", "") for n in plan.get("narration", [])).strip()
                or " ".join(s.get("_caption", "") for s in scenes).strip()
                or "Discover something amazing today."
            )
            paced_text = pace_narration(narr_text)
            voice      = VOICE_MAP.get(
                plan.get("voice_style", "professional_female"), "en-US-JennyNeural"
            )
            raw_audio  = tmp / "voice_raw.mp3"
            norm_audio = tmp / "voice.mp3"
            await asyncio.wait_for(
                generate_tts(paced_text, voice, str(raw_audio)), timeout=90
            )
            try:
                normalize_loudness(str(raw_audio), str(norm_audio))
            except Exception:
                import shutil
                shutil.copy(str(raw_audio), str(norm_audio))

            # Step 2 — Build scene visuals
            u("FETCHING_ASSETS", 15)
            words     = narr_text.split()
            wps       = max(1, len(words) // N_SCENES)
            headlines = [" ".join(words[i*wps:(i+1)*wps])[:55] for i in range(N_SCENES)]

            # Distribute narration text per scene for narration-aware timing
            scene_narrations: list[str] = []
            for i in range(N_SCENES):
                chunk = " ".join(words[i*wps:(i+1)*wps])
                scene_narrations.append(chunk)

            # BPM from plan (user can set "bpm": 128 in render config)
            bpm   = float(plan.get("bpm", 0.0))
            clips: list[str] = []

            for i, scene in enumerate(scenes):
                stype = scene.get("scene_type", SCENE_ORDER[i % N_SCENES])
                raw   = tmp / f"raw_{i}.mp4"
                proc  = str(tmp / f"proc_{i}.mp4")
                pct   = 15 + i * 6

                # ── Mode A: GPU text-to-video via Modal ────────────────────
                if mode == "ai" and MODAL_EP:
                    prompt = scene.get("visual_description", " ".join(
                        scene.get("search_keywords", [stype])[:3]
                    ))
                    async with httpx.AsyncClient(timeout=5) as c:
                        clip = await generate_ai_scene(c, prompt, i, SCENE_DUR, raw)
                    if clip and clip.exists():
                        ai_dur = get_duration(str(clip))
                        if ai_dur >= SCENE_DUR - 0.5:
                            # Full-length AI clip
                            trim_and_grade(str(clip), SCENE_DUR, proc, motion_idx=i)
                            clips.append(proc)
                        else:
                            # Short AI clip (<10s) — hybrid: AI clip + micro-shots pad
                            ai_proc = str(tmp / f"ai_proc_{i}.mp4")
                            trim_and_grade(str(clip), ai_dur, ai_proc, motion_idx=i)
                            pad_mp4 = await build_micro_scene(
                                i, scene, tmp, headlines,
                                scene_narration=scene_narrations[i], bpm=bpm,
                            )
                            remain   = SCENE_DUR - ai_dur
                            pad_trim = str(tmp / f"pad_trim_{i}.mp4")
                            trim_and_grade(pad_mp4, remain, pad_trim, motion_idx=i)
                            stitch_micro_shots([ai_proc, pad_trim], proc, add_sfx=False)
                            clips.append(proc)
                        u("FETCHING_ASSETS", pct)
                        continue

                # ── Mode stock: single Pexels clip per scene ───────────────
                if mode == "stock" and PEXELS_KEY:
                    kws = (scene.get("search_keywords")
                           or random.choice(
                               SCENE_KWMAP.get(stype, [["lifestyle"]])
                           ).split())
                    async with httpx.AsyncClient(timeout=30) as c:
                        clip = await fetch_pexels(c, kws, SCENE_DUR, raw)
                    if clip and clip.exists():
                        try:
                            trim_and_grade(str(clip), SCENE_DUR, proc, motion_idx=i)
                            clips.append(proc)
                            u("FETCHING_ASSETS", pct)
                            continue
                        except Exception:
                            pass

                # ── Mode motion (default): 8 AI micro-shots per scene ──────
                try:
                    scene_mp4 = await build_micro_scene(
                        i, scene, tmp, headlines,
                        scene_narration=scene_narrations[i],
                        bpm=bpm,
                    )
                    clips.append(scene_mp4)
                    u("FETCHING_ASSETS", pct)
                    continue
                except Exception as e:
                    print(f"[Mode B] micro-scene {i} failed: {e}")

                # ── Last resort: plain color card ──────────────────────────
                fallback_colors = {
                    "hook": "#0d0020", "problem": "#1a0000", "product": "#00101e",
                    "benefits": "#001a0a", "social_proof": "#0e0e00", "cta": "#1a0010",
                }
                clr = fallback_colors.get(stype, "#0a0a14")
                make_color_card(clr, SCENE_DUR, proc,
                                text=headlines[i] if i < len(headlines) else None)
                clips.append(proc)
                u("FETCHING_ASSETS", pct)

            # Step 3 — Compose 6 scenes with xfade → exactly 60.0s
            u("COMPOSITING", 55)
            composed = tmp / "composed.mp4"
            compose_xfade(clips, str(composed))

            # Step 4 — Optional music bed
            u("COMPOSITING", 60)
            music_path = await fetch_music(tmp)

            # Step 5 — Mix voice + music, pad to video duration
            u("COMPOSITING", 65)
            with_audio = tmp / "with_audio.mp4"
            mix_audio(str(composed), str(norm_audio), str(with_audio),
                      music_path=music_path, music_vol=0.10)

            # Step 6 — Generate word-level ASS karaoke captions (Whisper)
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

            # Step 7 — Burn ASS karaoke captions
            u("COMPOSITING", 85)
            final = tmp / "final.mp4"
            try:
                burn_ass_captions(str(with_audio), ass_path, str(final))
            except Exception:
                captions = build_word_captions(narr_text, vid_dur)
                burn_captions(str(with_audio), captions,
                              plan.get("caption_style", "bold"), str(final))

            # Step 8 — Upload to Cloudinary
            u("UPLOADING", 92)
            video_url = await upload_cloudinary(str(final), "video")
            thumb     = tmp / "thumb.jpg"
            extract_thumbnail(str(final), str(thumb))
            thumb_url = await upload_cloudinary(str(thumb), "image")

            dur  = get_duration(str(final))
            size = os.path.getsize(str(final))
            u("DONE", 100,
              video_url=video_url, thumbnail_url=thumb_url,
              duration_s=round(dur, 1), file_size_bytes=size,
              render_mode=mode, whisper_used=_WHISPER_AVAILABLE,
              micro_shots=(mode not in ("stock", "ai")))

        except asyncio.TimeoutError:
            u("FAILED", 0, error="TTS timed out after 90s")
        except Exception as e:
            u("FAILED", 0, error=str(e))
            raise
