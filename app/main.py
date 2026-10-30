"""ReelRelay: TikTok link -> AI caption -> approval -> Instagram Reels + YouTube Shorts."""
import hashlib
import hmac
import json
import re
import sys
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
ROOT = BASE.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402  (config.py: saari settings yahin se)

MEDIA = BASE / "media"
MEDIA.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIKTOK_RE = re.compile(r"https?://\S*tiktok\.com\S*", re.I)
MEDIA_MAX_AGE = 6 * 3600  # keep local files up to 6h so Buffer can fetch them
MAX_VIDEO_BYTES = 200 * 1024 * 1024  # safety limit for local storage

JOBS: dict[str, dict] = {}  # in-memory (server restart par reset)
FAILS: dict[str, list] = {}  # login rate-limit

CAPTION_SYSTEM = """You write engaging short-form video captions for Instagram Reels and YouTube Shorts.

Use the original description only to understand what the video is about. Write a FRESH caption in your own words: do not copy its sentences or hashtags.

Structure:
1. A strong one-line hook.
2. 1-3 short lines of value or context about the video.
3. One clear call to action (save, share, comment or follow).
4. A blank line, then 5-10 relevant hashtags.

Rules: match the brand voice, 1-3 emojis max, no fake statistics, keep the WHOLE caption under 800 characters. Output ONLY the caption."""


def cfg(key: str, default: str = "") -> str:
    """Read settings only from config.py; no environment-variable dependency."""
    return str(getattr(config, key, default) or default).strip()


def cleanup_media(max_age: int = MEDIA_MAX_AGE) -> None:
    cutoff = time.time() - max_age
    for f in MEDIA.glob("*.mp4"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def buffer_ready() -> bool:
    return bool(cfg("BUFFER_API_KEY"))


def catbox_ready() -> bool:
    return True  # Catbox public upload API does not require a key


app = FastAPI(title="ReelRelay")


# ------------------------------------------------------------------ login
def auth_token() -> str:
    key = (cfg("SECRET_KEY") or "reelrelay").encode()
    return hmac.new(key, cfg("APP_PASSWORD").encode(), hashlib.sha256).hexdigest()


def is_logged_in(request: Request) -> bool:
    if not cfg("APP_PASSWORD"):
        return False
    return hmac.compare_digest(request.cookies.get("rr_auth", ""), auth_token())


OPEN_PATHS = {"/api/auth", "/api/login"}


@app.middleware("http")
async def guard(request: Request, call_next):
    p = request.url.path
    # /media/* must stay public so Buffer can download the video without login
    if p.startswith("/media/"):
        return await call_next(request)
    if p.startswith("/api/") and p not in OPEN_PATHS:
        if not is_logged_in(request):
            return JSONResponse({"detail": "Pehle login karo."}, status_code=401)
    return await call_next(request)


class LoginIn(BaseModel):
    password: str


@app.get("/api/auth")
def auth_state(request: Request):
    return {"needs_password": True, "configured": bool(cfg("APP_PASSWORD")), "logged_in": is_logged_in(request)}


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    if len(FAILS[ip]) >= 8:
        raise HTTPException(429, "Bahut galat koshish. 10 minute baad try karo.")
    pw = cfg("APP_PASSWORD")
    if not pw:
        raise HTTPException(503, "Set APP_PASSWORD in config.py first.")
    if not hmac.compare_digest(body.password.encode(), pw.encode()):
        FAILS[ip].append(now)
        raise HTTPException(401, "Password galat hai.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("rr_auth", auth_token(), max_age=30 * 86400, httponly=True, samesite="lax",
                    secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
    return resp


# --------------------------------------------------------------- models
class UrlIn(BaseModel):
    url: str


class JobIn(BaseModel):
    job_id: str


class PublishIn(BaseModel):
    job_id: str
    caption: str
    instagram: bool = False
    youtube: bool = False
    instagram_channel_id: str | None = None
    youtube_channel_id: str | None = None
    scheduling_mode: str = "queue"  # queue | now
    rights_confirmed: bool = False


def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job nahi mila. Link dobara fetch karo.")
    return job


@app.get("/api/config")
def config_status():
    return {
        "llm": bool(cfg("LLM_API_KEY")),
        "buffer_key": buffer_ready(),
        "tikwm_key": bool(cfg("TIKWMAPI_KEY")),
        "self_hosted_media": True, "public_base_url": bool(cfg("PUBLIC_BASE_URL")),
        "publishing_provider": "buffer",
    }


def buffer_gql(query: str) -> dict:
    key = cfg("BUFFER_API_KEY")
    if not key:
        raise HTTPException(400, "config.py mein BUFFER_API_KEY bharo.")
    r = httpx.post("https://api.buffer.com", json={"query": query}, timeout=45,
                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise HTTPException(502, str(body["errors"])[:500])
    return body.get("data") or {}


@app.post("/api/buffer/channels")
def buffer_channels():
    try:
        orgs = buffer_gql("query { account { organizations { id } } }").get("account", {}).get("organizations", [])
        out = []
        for org in orgs:
            query = "query { channels(input: { organizationId: %s }) { id name service type isDisconnected isLocked } }" % json.dumps(org["id"])
            channels = buffer_gql(query).get("channels", [])
            out.extend([{"id": c["id"], "name": c.get("name") or "Unnamed channel",
                         "service": str(c.get("service") or "").lower(),
                         "type": c.get("type"), "isDisconnected": c.get("isDisconnected", False),
                         "isLocked": c.get("isLocked", False)} for c in channels])
        return {"channels": out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Buffer se channels nahi mile: {str(e)[:300]}")


# ----------------------------------------------------------- step 1: fetch

def reencode_for_reels(src: Path, dest: Path) -> None:
    """Force max 60 fps + H.264/AAC so Instagram Reels accepts the file."""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", "fps=60",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            # fallback: just copy if ffmpeg fails
            dest.write_bytes(src.read_bytes())
    except Exception:
        dest.write_bytes(src.read_bytes())


def public_media_url(job_id: str, request: Request | None = None) -> str:
    """Build a public URL that Buffer can fetch (self-hosted on this app)."""
    base = cfg("PUBLIC_BASE_URL").rstrip("/")
    if not base and request is not None:
        # Auto-detect from incoming request (works on Render)
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if host:
            base = f"{proto}://{host}".rstrip("/")
    if not base:
        raise HTTPException(
            400,
            "config.py mein PUBLIC_BASE_URL set karo (apna Render URL). "
            "Example: https://virello-xxxx.onrender.com"
        )
    return f"{base}/media/{job_id}.mp4"


def catbox_upload(path: Path) -> str:
    """Optional fallback: upload to Catbox if needed. Prefer public_media_url instead."""
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise HTTPException(413, "Video 200 MB se badi hai.")
    with open(path, "rb") as f:
        r = httpx.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (path.name, f, "video/mp4")},
            timeout=300,
        )
    r.raise_for_status()
    public_url = r.text.strip()
    if not public_url.startswith("https://files.catbox.moe/"):
        raise RuntimeError(public_url[:300] or "Catbox ne public file URL nahi diya.")
    return public_url


@app.post("/api/fetch")
def fetch_video(body: UrlIn, request: Request):
    cleanup_media()
    m = TIKTOK_RE.fullmatch(body.url.strip())
    if not m:
        raise HTTPException(400, "Abhi supported TikTok video URL daalo.")
    try:
        tikwm_key = cfg("TIKWMAPI_KEY")
        if not tikwm_key:
            raise HTTPException(400, "config.py mein TIKWMAPI_KEY bharo.")
        r = httpx.get(
            "https://api.tikwmapi.com/",
            params={"url": m.group(0), "hd": "1"},
            headers={**UA, "x-tikwmapi-key": tikwm_key, "Accept": "application/json"},
            timeout=60,
            verify=False,
        )
        if r.status_code == 429:
            raise HTTPException(429, "TikWM rate limit/quota reached. Thodi der baad try karo ya plan quota check karo.")
        r.raise_for_status()
        payload = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"TikWM API se jawab nahi aaya: {str(e)[:250]}")

    data = payload.get("data") or {}
    src = data.get("hdplay") or data.get("play")
    if payload.get("code") != 0 or not src:
        code = payload.get("code")
        message = payload.get("msg") or "Video nahi mili."
        if code == 10401:
            raise HTTPException(502, "TikWM API key invalid ya missing hai. TIKWMAPI_KEY check karo.")
        if code in (10429, 10430):
            raise HTTPException(429, f"TikWM quota/rate limit: {message}")
        raise HTTPException(404, message)
    if src.startswith("/"):
        src = "https://www.tikwm.com" + src

    job_id = uuid.uuid4().hex[:10]
    raw_path = MEDIA / f"{job_id}_raw.mp4"
    path = MEDIA / f"{job_id}.mp4"
    size = 0
    try:
        # verify=False: some TikTok CDN certs cause CERTIFICATE_VERIFY_FAILED on Render
        with httpx.stream("GET", src, headers=UA, timeout=180, follow_redirects=True, verify=False) as resp:
            resp.raise_for_status()
            content_length = int(resp.headers.get("content-length") or 0)
            if content_length > MAX_VIDEO_BYTES:
                raise HTTPException(413, "Video 200 MB se badi hai.")
            with open(raw_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_VIDEO_BYTES:
                        raise HTTPException(413, "Video 200 MB se badi hai.")
                    f.write(chunk)
        if size == 0:
            raise RuntimeError("Downloaded file khaali hai.")
        # Re-encode to max 60 fps + H.264 so Instagram Reels accepts it
        reencode_for_reels(raw_path, path)
        raw_path.unlink(missing_ok=True)
        public_url = public_media_url(job_id, request)
    except HTTPException:
        raw_path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise
    except Exception as e:
        raw_path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise HTTPException(502, f"Download fail hua: {str(e)[:250]}")

    author = data.get("author") or {}
    job = {"id": job_id, "title": data.get("title") or "", "creator": author.get("unique_id") or "",
           "duration": data.get("duration"), "video_url": f"/media/{job_id}.mp4",
           "catbox_url": public_url, "caption": "", "results": {}}
    JOBS[job_id] = job
    return job


# --------------------------------------------------------- step 2: caption
@app.post("/api/caption")
def make_caption(body: JobIn):
    job = get_job(body.job_id)
    key = cfg("LLM_API_KEY")
    if not key:
        raise HTTPException(400, "config.py mein LLM_API_KEY bharo.")
    user_msg = (f"Original TikTok description: {job['title'] or '(none)'}\n"
                f"Brand: {cfg('BRAND_NAME', 'Your Brand')}\n"
                f"Brand voice: {cfg('BRAND_VOICE', 'friendly, energetic, helpful')}")
    base_url = cfg("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    try:
        r = httpx.post(f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                       json={"model": cfg("LLM_MODEL", "openai/gpt-oss-20b:free"), "temperature": 0.7,
                             "messages": [{"role": "system", "content": CAPTION_SYSTEM},
                                          {"role": "user", "content": user_msg}]},
                       timeout=90)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        raise HTTPException(502, f"Caption nahi ban payi: {e}")
    if cfg("CREDIT_CREATOR", "yes").lower() == "yes" and job["creator"]:
        text += f"\n\n🎥 Credit: @{job['creator']} (TikTok)"
    job["caption"] = text[:2000]
    return {"caption": job["caption"]}


# --------------------------------------------------------- step 3: publish
def buffer_create_video_post(channel_id: str, service: str, video_url: str, caption: str,
                            title: str, scheduling_mode: str) -> str:
    """Create a video post in Buffer. ReelRelay never calls Meta/YouTube upload APIs."""
    service = service.lower()
    text = caption
    if service == "instagram":
        metadata = "metadata: { instagram: { type: reel, shouldShareToFeed: true } }"
    elif service == "youtube":
        clean_title = re.sub(r"#\w+", "", title.strip()).strip() or "New Short"
        clean_title = clean_title[:95]
        privacy = cfg("BUFFER_YOUTUBE_PRIVACY", "public").lower()
        if privacy not in {"public", "private", "unlisted"}:
            privacy = "public"
        metadata = ("metadata: { youtube: { title: %s, privacy: %s, categoryId: \"22\", "
                    "notifySubscribers: true, madeForKids: false } }" %
                    (json.dumps(clean_title, ensure_ascii=False), privacy))
    else:
        raise RuntimeError(f"Unsupported Buffer service: {service}")

    if scheduling_mode == "now":
        mode = "shareNow"
        scheduling = "automatic"
    else:
        mode = "addToQueue"
        scheduling = "automatic"

    query = f"""mutation CreatePost {{
      createPost(input: {{
        channelId: {json.dumps(channel_id)}
        text: {json.dumps(text, ensure_ascii=False)}
        schedulingType: {scheduling}
        mode: {mode}
        {metadata}
        assets: [{{ video: {{ url: {json.dumps(video_url)} }} }}]
      }}) {{
        ... on PostActionSuccess {{ post {{ id dueAt }} }}
        ... on MutationError {{ message }}
      }}
    }}"""
    data = buffer_gql(query)
    cp = data.get("createPost") or {}
    post = cp.get("post") or {}
    if not post.get("id"):
        raise RuntimeError(cp.get("message") or "Buffer post create nahi kar paya.")
    return f"Buffer accepted post (ID {post['id']}); mode: {'publish now' if scheduling_mode == 'now' else 'queue'}."


def run_publish(job: dict, body: PublishIn):
    steps = []
    if body.instagram:
        steps.append(("instagram", body.instagram_channel_id, "instagram"))
    if body.youtube:
        steps.append(("youtube", body.youtube_channel_id, "youtube"))
    for name, channel_id, service in steps:
        job["results"][name] = {"state": "running", "message": "Buffer ko bhej rahe hain…"}
        try:
            job["results"][name] = {"state": "done", "message": buffer_create_video_post(
                channel_id, service, job["catbox_url"], body.caption, job.get("title", ""), body.scheduling_mode)}
        except Exception as e:
            job["results"][name] = {"state": "error", "message": str(e)[:400]}
    if all(r["state"] == "done" for r in job["results"].values()):
        (MEDIA / f"{job['id']}.mp4").unlink(missing_ok=True)


@app.post("/api/publish")
def publish(body: PublishIn, background: BackgroundTasks):
    job = get_job(body.job_id)
    if not body.rights_confirmed:
        raise HTTPException(400, "Confirm karo ki video par tumhare paas rights/permission hai.")
    if not (body.instagram or body.youtube):
        raise HTTPException(400, "Kam se kam ek Buffer channel chuno.")
    if not cfg("BUFFER_API_KEY"):
        raise HTTPException(400, "config.py mein BUFFER_API_KEY bharo.")
    if not body.caption.strip():
        raise HTTPException(400, "Caption khaali hai.")
    if body.instagram and not body.instagram_channel_id:
        raise HTTPException(400, "Instagram ka Buffer channel select karo.")
    if body.youtube and not body.youtube_channel_id:
        raise HTTPException(400, "YouTube ka Buffer channel select karo.")
    # Validate channel IDs server-side; don't trust client-supplied platform/channel pairing.
    available = buffer_channels().get("channels", [])
    if body.instagram:
        channel = next((c for c in available if c["id"] == body.instagram_channel_id), None)
        if not channel or channel["service"] != "instagram":
            raise HTTPException(400, "Selected channel Instagram channel nahi hai. Buffer channels reload karo.")
        if channel.get("isDisconnected") or channel.get("isLocked"):
            raise HTTPException(400, "Selected Instagram channel disconnected ya locked hai.")
    if body.youtube:
        channel = next((c for c in available if c["id"] == body.youtube_channel_id), None)
        if not channel or channel["service"] != "youtube":
            raise HTTPException(400, "Selected channel YouTube channel nahi hai. Buffer channels reload karo.")
        if channel.get("isDisconnected") or channel.get("isLocked"):
            raise HTTPException(400, "Selected YouTube channel disconnected ya locked hai.")
    if body.scheduling_mode not in {"queue", "now"}:
        raise HTTPException(400, "Invalid scheduling mode.")
    job["caption"] = body.caption
    job["results"] = {n: {"state": "queued", "message": "Buffer queue mein bhejne ki taiyari…"}
                      for n, on in (("instagram", body.instagram), ("youtube", body.youtube)) if on}
    background.add_task(run_publish, job, body)
    return job


@app.get("/api/jobs/{job_id}")

@app.post("/api/delete")
def delete_job(body: JobIn):
    """Delete a job and its local video file."""
    job = JOBS.pop(body.job_id, None)
    for suffix in ("", "_raw"):
        (MEDIA / f"{body.job_id}{suffix}.mp4").unlink(missing_ok=True)
    return {"ok": True, "deleted": body.job_id if job else None}


def job_status(job_id: str):
    return get_job(job_id)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


cleanup_media()
if (BASE / "static" / "assets").exists():
    app.mount("/assets", StaticFiles(directory=BASE / "static" / "assets"), name="assets")
app.mount("/media", StaticFiles(directory=MEDIA), name="media")
