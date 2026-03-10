

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp, os, re, tempfile, requests

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

DOWNLOAD_DIR = tempfile.mkdtemp()

# ── Platform detection ─────────────────────────────────────────────────────
def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "tiktok.com" in u or "vm.tiktok" in u or "vt.tiktok" in u: return "tiktok"
    if "instagram.com" in u: return "instagram"
    if "facebook.com" in u or "fb.watch" in u: return "facebook"
    return "unknown"

# ── yt-dlp per-platform options ────────────────────────────────────────────
def get_opts(platform: str) -> dict:
    base = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
    }

    if platform == "youtube":
        base.update({
            "extractor_args": {"youtube": {"player_client": ["ios"]}},
            "http_headers": {
                "User-Agent": "com.google.ios.youtube/19.09.3 (iPhone14,3; U; CPU iOS 16_1 like Mac OS X)",
            }
        })

    elif platform == "tiktok":
        base.update({
            "extractor_args": {
                "tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"},
            },
            "http_headers": {
                "User-Agent": "TikTok 26.2.0 rv:262018 (iPhone; iOS 14.4.2; en_US) Cronet",
            }
        })

    elif platform in ("facebook", "instagram"):
        base.update({
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/122.0.0.0 Safari/537.36",
            }
        })

    return base


# ── Format duration ────────────────────────────────────────────────────────
def fmt_duration(secs):
    if not secs: return None
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def fmt_views(v):
    if not v: return None
    if v >= 1_000_000: return f"{v/1_000_000:.1f}M views"
    if v >= 1_000: return f"{v/1_000:.1f}K views"
    return f"{v} views"


# ── Core scraper ───────────────────────────────────────────────────────────
def scrape_info(url: str) -> dict:
    platform = detect_platform(url)
    opts = get_opts(platform)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # Handle playlists
    if info.get("entries"):
        info = list(info["entries"])[0]

    formats_raw = info.get("formats", [])
    formats, seen = [], set()

    for f in formats_raw:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        res    = f.get("height")
        furl   = f.get("url", "")
        fsize  = f.get("filesize") or f.get("filesize_approx")

        if not furl or furl.startswith("manifest"):
            continue

        # Video with audio (best for FB/IG/TT)
        if vcodec != "none" and acodec != "none" and res and furl:
            label = f"MP4 {res}p"
            if label not in seen:
                seen.add(label)
                formats.append({"label": label, "ext": "mp4", "url": furl,
                                 "filesize": fsize, "has_audio": True})

        # Video only
        elif vcodec != "none" and acodec == "none" and res and furl:
            label = f"MP4 {res}p (video)"
            if label not in seen:
                seen.add(label)
                formats.append({"label": f"MP4 {res}p", "ext": "mp4", "url": furl,
                                 "filesize": fsize, "has_audio": False})

        # Audio only
        elif vcodec == "none" and acodec != "none" and furl:
            label = "MP3 Audio"
            if label not in seen:
                seen.add(label)
                formats.append({"label": label, "ext": "mp3", "url": furl,
                                 "filesize": fsize})

    # Sort: video+audio first, then by resolution desc, audio last
    def sort_key(f):
        nums = re.findall(r'\d+', f["label"])
        res_num = int(nums[0]) if nums else 0
        is_audio = f["label"] == "MP3 Audio"
        has_audio = f.get("has_audio", True)
        return (is_audio, not has_audio, -res_num)

    formats.sort(key=sort_key)

    return {
        "title":    info.get("title", "Video"),
        "thumb":    info.get("thumbnail"),
        "author":   info.get("uploader") or info.get("channel"),
        "duration": fmt_duration(info.get("duration")),
        "views":    fmt_views(info.get("view_count")),
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
        raise HTTPException(400, "URL නෑ")
    if detect_platform(url) == "unknown":
        raise HTTPException(422, "YouTube, TikTok, Instagram හෝ Facebook link එකක් paste කරන්න")
    try:
        return scrape_info(url)
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        if "Sign in" in err or "bot" in err or "confirm" in err:
            raise HTTPException(422, "YouTube: bot check — ටිකක් delay කරලා try කරන්න")
        if "private" in err.lower():
            raise HTTPException(422, "Private video — download කරන්න බෑ")
        if "available" in err.lower() or "status code 0" in err:
            raise HTTPException(422, "Video available නෑ — link check කරන්න")
        raise HTTPException(422, err[:250])
    except Exception as e:
        raise HTTPException(500, str(e)[:250])


class StreamRequest(BaseModel):
    url: str
    ext: str = "mp4"
    filename: str = "saveit_video"

@app.post("/api/stream")
async def stream_file(req: StreamRequest):
    try:
        s = requests.Session()
        s.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        )
        r = s.get(req.url, stream=True, timeout=30)
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
    return {"status": "ok", "engine": "yt-dlp v4"}

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
