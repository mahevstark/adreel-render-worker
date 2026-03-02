# AdReel Render Worker

Python FastAPI worker that renders 60s vertical reels.

## Stack
- **TTS**: edge-tts (Microsoft neural voices, FREE, no API key)
- **B-roll**: Pexels API (200 req/hour free)
- **Compositing**: MoviePy + FFmpeg
- **Captions**: FFmpeg drawtext filter
- **Storage**: Cloudflare R2 (S3-compatible)

## Provider Options (cheapest first)

| Provider | GPU | Cost | Best for |
|---|---|---|---|
| **Modal** | A10G | $0.000612/sec (~$2.20/hr) | Best value, serverless |
| **RunPod** | A40 | $0.49/hr spot | Heavy volume |
| **Hugging Face Spaces** | T4 (free) | Free | Testing only |
| **Railway** | CPU only | $5/mo | No GPU, slow |
| **Self-hosted VPS** | CPU | ~$20/mo | No GPU, production |

## Viral Reel Style Rules (enforced)
- Max scene duration: 6 seconds
- Hook must appear in first 3 seconds
- Minimum 10 cuts per 60s
- CTA starts at 55s
- Max 3 words per caption token
- 120px safe margins from edges

## Env vars needed

```
PEXELS_API_KEY=your_pexels_key
RENDER_WORKER_SECRET=random_secret_string
R2_ENDPOINT=https://account_id.r2.cloudflarestorage.com
R2_ACCESS_KEY=your_r2_access_key
R2_SECRET_KEY=your_r2_secret_key
R2_BUCKET=adreel-renders
R2_PUBLIC_URL=https://pub-xxxx.r2.dev
```

## Deploy to Modal (recommended)

```bash
pip install modal
modal token new
modal deploy modal_worker.py
```

## Deploy with Docker

```bash
docker build -t adreel-worker .
docker run -p 8000:8000 --env-file .env adreel-worker
```

## Next.js env vars (in Vercel)

```
RENDER_WORKER_URL=https://your-worker-url.modal.run
RENDER_WORKER_SECRET=same_secret_as_above
```
