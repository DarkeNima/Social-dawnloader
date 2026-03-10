

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp
import os, re, tempfile, requests

app = FastAPI(docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Serve frontend
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

DOWNLOAD_DIR = tempfile.mkdtemp()

# ── yt-dlp options ─────────────────────────────────────────────────────────
def get_ydl_opts(extra={}):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Bypass bot detection
        "extractor_args": {
            "youtube": {"player_client": ["android", "web"]},
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/90.0.4430.91 Mobile Safari/537.36",
        },
    }
    opts.update(extra)
    return opts


# ── Platform detection ─────────────────────────────────────────────────────
def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:  return "youtube"
    if "tiktok.com"  in u or "vm.tiktok" in u or "vt.tiktok" in u: return "tiktok"
    if "instagram.com" in u:                    return "instagram"
    if "facebook.com" in u or "fb.watch" in u:  return "facebook"
    return "unknown"


# ── Info scraper using yt-dlp ──────────────────────────────────────────────
def scrape_info(url: str) -> dict:
    platform = detect_platform(url)

    with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    # Build format list
    formats, seen = [], set()
    
    # Check if it's a playlist/multiple entries
    entries = info.get("entries")
    if entries:
        info = entries[0]

    for f in info.get("formats", []):
        vcodec  = f.get("vcodec", "none")
        acodec  = f.get("acodec", "none")
        res     = f.get("height")
        fid     = f.get("format_id")
        furl    = f.get("url", "")
        ext     = f.get("ext", "mp4")
        fsize   = f.get("filesize") or f.get("filesize_approx")

        # Video formats
        if vcodec != "none" and res and furl:
            label = f"MP4 {res}p"
            if label not in seen:
                seen.add(label)
                formats.append({
                    "label":    label,
                    "ext":      "mp4",
                    "url":      furl,
                    "filesize": fsize,
                    "format_id": fid,
                })

        # Audio only
        elif vcodec == "none" and acodec != "none" and furl:
            label = "MP3 Audio"
            if label not in seen:
                seen.add(label)
                formats.append({
                    "label":    label,
                    "ext":      "mp3",
                    "url":      furl,
                    "filesize": fsize,
                    "format_id": fid,
                })

    # Sort: highest res first, audio last
    def sort_key(f):
        nums = re.findall(r'\d+', f["label"])
        return (f["label"] == "MP3 Audio", -(int(nums[0]) if nums else 0))
    formats.sort(key=sort_key)

    # Duration formatting
    dur = info.get("duration")
    dur_str = None
    if dur:
        m, s = divmod(int(dur), 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # View count formatting
    views = info.get("view_count")
    views_str = None
    if views:
        if views >= 1_000_000: views_str = f"{views/1_000_000:.1f}M views"
        elif views >= 1_000:   views_str = f"{views/1_000:.1f}K views"
        else:                  views_str = f"{views} views"

    return {
        "title":    info.get("title", "Video"),
        "thumb":    info.get("thumbnail"),
        "author":   info.get("uploader") or info.get("channel"),
        "duration": dur_str,
        "views":    views_str,
        "platform": platform,
        "formats":  formats[:8],
    }


# ── Routes ─────────────────────────────────────────────────────────────────
class InfoRequest(BaseModel):
    url: str

@app.post("/api/info")
async def get_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "No URL provided")
    if detect_platform(url) == "unknown":
        raise HTTPException(422, "Unsupported platform")
    try:
        return scrape_info(url)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in" in msg or "bot" in msg:
            raise HTTPException(422, "YouTube bot check — try a different video")
        if "Private" in msg or "private" in msg:
            raise HTTPException(422, "This video is private")
        raise HTTPException(422, msg[:200])
    except Exception as e:
        raise HTTPException(500, str(e)[:200])


class StreamRequest(BaseModel):
    url: str
    ext: str = "mp4"
    filename: str = "saveit_video"

@app.post("/api/stream")
async def stream_file(req: StreamRequest):
    """Stream video directly to browser."""
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
                          "AppleWebKit/537.36 Chrome/90.0.4430.91 Mobile Safari/537.36"
        })
        r = session.get(req.url, stream=True, timeout=30)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", f"video/{req.ext}")
        def gen():
            for chunk in r.iter_content(65536):
                yield chunk
        return StreamingResponse(gen(), media_type=ct, headers={
            "Content-Disposition": f'attachment; filename="{req.filename}.{req.ext}"'
        })
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/health")
async def health():
    return {"status": "ok", "engine": "yt-dlp"}

@app.get("/")
async def root():
    index = os.path.join(static_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"status": "SaveIt API running"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
