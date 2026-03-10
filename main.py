
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp, os, re, tempfile, requests, asyncio

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

DOWNLOAD_DIR = tempfile.mkdtemp()

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "tiktok.com" in u or "vm.tiktok" in u or "vt.tiktok" in u: return "tiktok"
    if "instagram.com" in u: return "instagram"
    if "facebook.com" in u or "fb.watch" in u: return "facebook"
    return "unknown"

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

def fmt_size(b):
    if not b: return ""
    if b >= 1_000_000: return f"~{b/1_000_000:.0f}MB"
    if b >= 1_000: return f"~{b/1_000:.0f}KB"
    return ""


# ── YouTube info — try multiple clients ───────────────────────────────────
def scrape_youtube(url):
    clients = ["ios", "mweb", "android", "tv_embedded"]
    last_err = ""
    for client in clients:
        try:
            opts = {
                "quiet": True, "no_warnings": True, "skip_download": True,
                "extractor_args": {"youtube": {"player_client": [client]}},
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return build_result(info, "youtube")
        except Exception as e:
            last_err = str(e)
            continue
    raise Exception(last_err)


# ── TikTok info ────────────────────────────────────────────────────────────
def scrape_tiktok(url):
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extractor_args": {
            "tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"},
        },
        "http_headers": {
            "User-Agent": "TikTok 26.2.0 rv:262018 (iPhone; iOS 14.4.2; en_US) Cronet",
        }
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return build_result(info, "tiktok")


# ── Facebook / Instagram — download+merge then stream ─────────────────────
def scrape_fb_ig(url, platform):
    """
    For FB/IG: yt-dlp download best quality (merges audio+video)
    Returns a special 'download' type format pointing to /api/download
    """
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        }
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("entries"):
        info = list(info["entries"])[0]

    # Build formats — prefer combined audio+video
    formats, seen = [], set()
    for f in info.get("formats", []):
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        res    = f.get("height")
        furl   = f.get("url", "")
        fsize  = f.get("filesize") or f.get("filesize_approx")
        if not furl: continue

        # Combined stream (has both video + audio) — best for direct download
        if vcodec != "none" and acodec != "none" and res:
            label = f"MP4 {res}p"
            if label not in seen:
                seen.add(label)
                formats.append({
                    "label": label, "ext": "mp4",
                    "url": furl, "filesize": fsize,
                    "sub": fmt_size(fsize),
                })

    # If no combined streams found, use /api/download to merge server-side
    if not formats:
        formats = [
            {"label": "MP4 Best Quality", "ext": "mp4",
             "url": None, "download_url": url, "merge": True,
             "sub": "server-side merge"},
            {"label": "MP4 SD", "ext": "mp4",
             "url": None, "download_url": url + "::sd", "merge": True,
             "sub": "smaller file"},
        ]

    return {
        "title":    info.get("title", "Video"),
        "thumb":    info.get("thumbnail"),
        "author":   info.get("uploader") or info.get("channel"),
        "duration": fmt_duration(info.get("duration")),
        "views":    fmt_views(info.get("view_count")),
        "platform": platform,
        "formats":  formats[:6],
    }


# ── Generic format builder ─────────────────────────────────────────────────
def build_result(info, platform):
    if info.get("entries"):
        info = list(info["entries"])[0]

    formats, seen = [], set()
    for f in info.get("formats", []):
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        res    = f.get("height")
        furl   = f.get("url", "")
        fsize  = f.get("filesize") or f.get("filesize_approx")
        if not furl: continue

        if vcodec != "none" and res:
            label = f"MP4 {res}p"
            if label not in seen:
                seen.add(label)
                formats.append({"label": label, "ext": "mp4", "url": furl,
                                 "filesize": fsize, "sub": fmt_size(fsize)})
        elif vcodec == "none" and acodec != "none":
            if "MP3 Audio" not in seen:
                seen.add("MP3 Audio")
                formats.append({"label": "MP3 Audio", "ext": "mp3", "url": furl,
                                 "filesize": fsize, "sub": fmt_size(fsize)})

    def sort_key(f):
        nums = re.findall(r'\d+', f["label"])
        return (f["label"] == "MP3 Audio", -(int(nums[0]) if nums else 0))
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
    platform = detect_platform(url)
    if platform == "unknown":
        raise HTTPException(422, "YouTube, TikTok, Instagram හෝ Facebook link paste කරන්න")
    try:
        if platform == "youtube":
            return scrape_youtube(url)
        elif platform == "tiktok":
            return scrape_tiktok(url)
        else:
            return scrape_fb_ig(url, platform)
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        if "Sign in" in err or "bot" in err: raise HTTPException(422, "YouTube bot check — ටිකක් delay කරලා try කරන්න")
        if "private" in err.lower(): raise HTTPException(422, "Private video")
        if "status code 0" in err or "available" in err.lower(): raise HTTPException(422, "Video available නෑ — full TikTok link paste කරන්න")
        raise HTTPException(422, err[:250])
    except Exception as e:
        raise HTTPException(500, str(e)[:250])


# ── Server-side download+merge for FB/IG ──────────────────────────────────
class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"

@app.post("/api/download")
async def download_merge(req: DownloadRequest):
    """Download + merge audio+video server-side, stream back."""
    url = req.url.replace("::sd", "")
    quality = "worstvideo+worstaudio/worst" if "::sd" in req.url else "bestvideo+bestaudio/best"

    out_tmpl = os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s")
    opts = {
        "quiet": True, "no_warnings": True,
        "format": quality,
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        }
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not filename.endswith(".mp4"):
                filename = os.path.splitext(filename)[0] + ".mp4"

        if not os.path.exists(filename):
            raise HTTPException(500, "File not created")

        def gen():
            with open(filename, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk
            os.unlink(filename)  # cleanup

        title = info.get("title", "video")[:40]
        safe_title = re.sub(r'[^\w\s-]', '', title).strip()

        return StreamingResponse(gen(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{safe_title}.mp4"',
            "Content-Length": str(os.path.getsize(filename)),
        })
    except Exception as e:
        raise HTTPException(500, str(e)[:200])


@app.get("/api/health")
async def health():
    return {"status": "ok", "engine": "yt-dlp v5"}

@app.get("/")
async def root():
    index = os.path.join(static_dir, "index.html")
    return FileResponse(index) if os.path.exists(index) else {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
