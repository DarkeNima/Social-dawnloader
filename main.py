
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

def expand_url(url: str) -> str:
    """Expand short URLs like vt.tiktok.com"""
    try:
        r = requests.head(url, allow_redirects=True, timeout=10,
            headers={"User-Agent": "Mozilla/5.0"})
        return r.url
    except:
        return url

def build_formats(info):
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
                formats.append({"label": label, "ext": "mp4",
                                 "url": furl, "sub": fmt_size(fsize),
                                 "has_audio": acodec != "none"})
        elif vcodec == "none" and acodec != "none":
            if "MP3 Audio" not in seen:
                seen.add("MP3 Audio")
                formats.append({"label": "MP3 Audio", "ext": "mp3",
                                 "url": furl, "sub": fmt_size(fsize)})

    def sort_key(f):
        nums = re.findall(r'\d+', f["label"])
        return (f["label"] == "MP3 Audio",
                not f.get("has_audio", True),
                -(int(nums[0]) if nums else 0))
    formats.sort(key=sort_key)
    return formats[:8]

def make_result(info, platform):
    if info.get("entries"):
        info = list(info["entries"])[0]
    return {
        "title":    info.get("title", "Video"),
        "thumb":    info.get("thumbnail"),
        "author":   info.get("uploader") or info.get("channel"),
        "duration": fmt_duration(info.get("duration")),
        "views":    fmt_views(info.get("view_count")),
        "platform": platform,
        "formats":  build_formats(info),
    }

# ── Scrapers ───────────────────────────────────────────────────────────────

def scrape_youtube(url):
    for client in ["ios", "mweb", "android", "tv_embedded"]:
        try:
            opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                    "extractor_args": {"youtube": {"player_client": [client]}}}
            with yt_dlp.YoutubeDL(opts) as ydl:
                return make_result(ydl.extract_info(url, download=False), "youtube")
        except Exception as e:
            last = str(e)
    raise Exception(last)

def scrape_tiktok(url):
    # Expand short URL first
    expanded = expand_url(url)
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
        return make_result(ydl.extract_info(expanded, download=False), "tiktok")

def scrape_fb_ig(url, platform):
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        # Prefer single-file format (has audio already, no ffmpeg needed)
        "format": "best[ext=mp4]/best/bestvideo+bestaudio",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        }
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("entries"):
        info = list(info["entries"])[0]

    # Get selected format URL
    requested = info.get("requested_formats") or []
    direct_url = info.get("url")

    formats = []

    # If single combined format — direct download
    if direct_url and not requested:
        res = info.get("height", "")
        fsize = info.get("filesize") or info.get("filesize_approx")
        formats.append({
            "label": f"MP4 {res}p" if res else "MP4 Best",
            "ext": "mp4", "url": direct_url,
            "sub": fmt_size(fsize),
        })
    else:
        # Multiple formats need merging — use /api/download
        formats.append({
            "label": "MP4 Best Quality", "ext": "mp4",
            "url": None, "download_url": url, "merge": True,
            "sub": "audio+video merge",
        })
        formats.append({
            "label": "MP4 SD", "ext": "mp4",
            "url": None, "download_url": url + "::sd", "merge": True,
            "sub": "smaller file",
        })

    # Also add any other direct formats from info
    for f in info.get("formats", [])[-5:]:
        vcodec = f.get("vcodec","none")
        acodec = f.get("acodec","none")
        res = f.get("height")
        furl = f.get("url","")
        if vcodec != "none" and acodec != "none" and res and furl:
            label = f"MP4 {res}p"
            if not any(x["label"] == label for x in formats):
                fsize = f.get("filesize") or f.get("filesize_approx")
                formats.append({"label": label, "ext": "mp4",
                                 "url": furl, "sub": fmt_size(fsize)})

    return {
        "title":    info.get("title", "Video"),
        "thumb":    info.get("thumbnail"),
        "author":   info.get("uploader") or info.get("channel"),
        "duration": fmt_duration(info.get("duration")),
        "views":    fmt_views(info.get("view_count")),
        "platform": platform,
        "formats":  formats[:6],
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
        if platform == "youtube":   return scrape_youtube(url)
        elif platform == "tiktok":  return scrape_tiktok(url)
        else:                       return scrape_fb_ig(url, platform)
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        if "Sign in" in err or "bot" in err: raise HTTPException(422, "YouTube bot check — ටිකක් later try කරන්න")
        if "private" in err.lower():         raise HTTPException(422, "Private video")
        if "status code 0" in err:           raise HTTPException(422, "Video available නෑ")
        raise HTTPException(422, err[:200])
    except Exception as e:
        raise HTTPException(500, str(e)[:200])


class DownloadRequest(BaseModel):
    url: str

@app.post("/api/download")
async def download_merge(req: DownloadRequest):
    """Download + ffmpeg merge server-side, stream back."""
    url = req.url.replace("::sd", "")
    quality = "worstvideo+worstaudio/worst" if req.url.endswith("::sd") else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

    out_tmpl = os.path.join(DOWNLOAD_DIR, "%(id)s_%(epoch)s.%(ext)s")
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
            fname = ydl.prepare_filename(info)
            if not fname.endswith(".mp4"):
                fname = os.path.splitext(fname)[0] + ".mp4"

        if not os.path.exists(fname):
            raise HTTPException(500, "File not created")

        size = os.path.getsize(fname)
        title = re.sub(r'[^\w\s-]', '', info.get("title","video")[:40]).strip()

        def gen():
            with open(fname, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk
            try: os.unlink(fname)
            except: pass

        return StreamingResponse(gen(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{title}.mp4"',
            "Content-Length": str(size),
        })
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e)[:200])


@app.get("/api/health")
async def health():
    import shutil
    return {"status": "ok", "ffmpeg": bool(shutil.which("ffmpeg"))}

@app.get("/")
async def root():
    index = os.path.join(static_dir, "index.html")
    return FileResponse(index) if os.path.exists(index) else {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
