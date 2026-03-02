# AdReel Render Worker

Python FastAPI worker that renders 60s vertical reels from RenderPlan JSON.

## Stack
- **TTS**: edge-tts (Microsoft neural voices — FREE, no API key)
- **B-roll**: Pexels API (free tier: 200 req/hour)
- **Compositing**: MoviePy + FFmpeg
- **Captions**: FFmpeg drawtext filter
- **Storage**: Cloudflare R2 (S3-compatible)

## One-click Deploy (Railway)

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/new?repo=https://github.com/mahevstark/adreel-render-worker)

### Manual Railway Deploy
1. Go to https://railway.app/new → Deploy from GitHub
2. Select this repo
3. Add env vars (see below)
4. Deploy

## Required Env Vars

```
RENDER_WORKER_SECRET=your_random_secret_here
PEXELS_API_KEY=your_pexels_api_key
R2_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com
R2_ACCESS_KEY=your_r2_access_key
R2_SECRET_KEY=your_r2_secret_key
R2_BUCKET=adreel-renders
R2_PUBLIC_URL=https://pub-xxxx.r2.dev
PORT=8000
```

## Then set in Vercel (adreel-studio project)

```
RENDER_WORKER_URL=https://your-worker.railway.app
RENDER_WORKER_SECRET=same_secret_as_above
```

## API Endpoints

- `POST /render/start` — Start render job
- `GET /render/status?id=JOB_ID` — Poll status
- `GET /render/result?id=JOB_ID` — Get final result
- `GET /health` — Health check

## Provider Comparison (cheapest first)

| Provider | GPU | Cost | Notes |
|---|---|---|---|
| Railway | CPU | $5/mo hobby | Fast deploy, no GPU |
| Koyeb | CPU | Free tier | No GPU |
| Hugging Face Spaces | T4 GPU | Free | Best for GPU |
| Modal | A10G | $0.000612/s | Best value GPU |
| RunPod | A40 | $0.49/hr spot | Heavy volume |

## Local Dev

```bash
cd worker
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
