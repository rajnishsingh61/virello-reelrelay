# ReelRelay — React + Python + Catbox + Buffer

**Flow:** supported TikTok URL → Python downloads video → Catbox public URL → AI caption + hashtags → user reviews and approves → Buffer schedules/publishes to selected Instagram and YouTube channels.

ReelRelay does **not** publish through Meta Graph API or YouTube Data API. Both platforms go through the user's Buffer account.

## Configure settings (config.py only)

1. Open `config.py`.
2. Fill in `APP_PASSWORD`, `SECRET_KEY`, `TIKWMAPI_KEY`, `BUFFER_API_KEY`, and `LLM_API_KEY` as needed.
3. Keep the repository private and never publish real credentials in a public GitHub repository.
4. Deploy the project to Render using the included Dockerfile and `render.yaml`. No Render Environment Variables are required by the app.

All application settings are read from `config.py`; environment variables are intentionally not used. `SECRET_KEY` should be a long, random string.

## Local run

Requirements: Python 3.12+ and Node.js 22+.

```bash
cd frontend
npm install
npm run build
cd ..
pip install -r requirements.txt
APP_PASSWORD='set-a-strong-password' SECRET_KEY='replace-with-a-long-random-secret' BUFFER_API_KEY='your-buffer-api-key' LLM_API_KEY='your-llm-key' uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## Important notes

- Catbox upload is capped at 200 MB by the app. The Catbox public URL must remain reachable until Buffer publishes the scheduled post.
- Buffer's API fetches media from a public URL; it does not accept a direct file upload.
- YouTube Shorts are created through the connected YouTube channel in Buffer. Actual publishing depends on Buffer account/channel eligibility, channel connection status, supported post fields, and video validation.
- `JOBS` is still in-memory. Restarting the service clears active job/status history; use a database/queue before scaling this into a multi-user production service.
- The source downloader uses `https://api.tikwmapi.com/` with `url` and `hd=1`, and requires the server-side `TIKWMAPI_KEY` header. TikWM quota/rate limits or service changes can make downloads fail. Only repost videos you own or have permission to use.
- Do not commit real API keys or passwords to a public GitHub repository. Since this project reads credentials from `config.py`, keep the repository private and protect the file.
