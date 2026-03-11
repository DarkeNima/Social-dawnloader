
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests, os, re, tempfile, urllib.parse

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

DOWNLOAD_DIR = tempfile.mkdtemp()

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Origin":  "https://ojoas.vercel.app",
    "Referer": "https://ojoas.vercel.app/",
})

OJOAS = "https://ojoas.vercel.app/api"

def detect_platform(url):
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "tiktok.com" in u or "vm.tiktok" in u or "vt.tiktok" in u: return "tiktok"
    if "instagram.com" in u: return "instagram"
    if "facebook.com" in u or "fb.watch" in u: return "facebook"
    if "twitter.com" in u or "x.com" in u: return "twitter"
    return "unknown"

def expand_url(url):
    try:
        r = S.head(url, allow_redirects=True, timeout=8)
        return r.url
    except:
        return url

def fmt_dur(s):
    if not s: return None
    s = int(s)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# ── TikTok via tikwm (most reliable) ──────────────────────────────────────
def scrape_tiktok(url):
    expanded = expand_url(url)
    
    # Try tikwm first
    try:
        r = S.post("https://www.tikwm.com/api/", 
                   data={"url": expanded, "hd": 1}, timeout=15)
        d = r.json()
        if d.get("code") == 0:
            data = d["data"]
            formats = []
            if data.get("hdplay"):
                formats.append({"label": "MP4 HD (No Watermark)", "ext": "mp4",
                                 "url": data["hdplay"], "sub": "HD • No watermark"})
            if data.get("play"):
                formats.append({"label": "MP4 SD (No Watermark)", "ext": "mp4",
                                 "url": data["play"], "sub": "SD • No watermark"})
            if data.get("wmplay"):
                formats.append({"label": "MP4 Watermark", "ext": "mp4",
                                 "url": data["wmplay"], "sub": "With watermark"})
            if data.get("music"):
                formats.append({"label": "MP3 Audio", "ext": "mp3",
                                 "url": data["music"], "sub": "Audio only"})
            if formats:
                return {
                    "title":    data.get("title", "TikTok Video"),
                    "thumb":    data.get("cover"),
                    "author":   data.get("author", {}).get("nickname"),
                    "duration": fmt_dur(data.get("duration")),
                    "views":    None,
                    "platform": "tiktok",
                    "formats":  formats,
                }
    except Exception as e:
        pass

    # Fallback: ojoas TikTok API
    r = S.get(f"{OJOAS}/tk?url={urllib.parse.quote(expanded)}", timeout=15)
    d = r.json()
    formats = []
    if d.get("data"):
        vlinks = d["data"].get("links", {}).get("video", [])
        alinks = d["data"].get("links", {}).get("audio", [])
        for i, v in enumerate(vlinks[:2]):
            if v.get("url"):
                formats.append({"label": f"MP4 {'HD' if i==0 else 'SD'}", "ext": "mp4",
                                 "url": v["url"], "sub": v.get("size","")})
        for a in alinks[:1]:
            if a.get("url"):
                formats.append({"label": "MP3 Audio", "ext": "mp3",
                                 "url": a["url"], "sub": "Audio only"})
    elif d.get("video"):
        for v in (d["video"] if isinstance(d["video"], list) else [d["video"]])[:2]:
            formats.append({"label": "MP4 Video", "ext": "mp4", "url": v, "sub": ""})
        if d.get("audio"):
            formats.append({"label": "MP3 Audio", "ext": "mp3",
                             "url": d["audio"][0] if isinstance(d["audio"], list) else d["audio"],
                             "sub": "Audio only"})

    if not formats:
        raise ValueError("TikTok: No download links found")

    return {
        "title":    d.get("data", {}).get("title") or d.get("title") or "TikTok Video",
        "thumb":    d.get("data", {}).get("thumbnail") or d.get("thumbnail"),
        "author":   None, "duration": None, "views": None,
        "platform": "tiktok", "formats": formats,
    }


# ── YouTube via ojoas API ──────────────────────────────────────────────────
def scrape_youtube(url):
    r = S.get(f"{OJOAS}/yt?url={urllib.parse.quote(url)}", timeout=20)
    if not r.ok:
        raise ValueError(f"YouTube API error: {r.status_code}")
    d = r.json()
    if not d.get("status"):
        raise ValueError(d.get("message", "YouTube fetch failed"))

    formats = []
    if d.get("mp4"):
        formats.append({"label": "MP4 360p", "ext": "mp4",
                         "url": d["mp4"], "sub": "360p"})
    if d.get("mp3"):
        formats.append({"label": "MP3 Audio", "ext": "mp3",
                         "url": d["mp3"], "sub": "Audio only"})

    if not formats:
        raise ValueError("YouTube: No download links found")

    return {
        "title":    d.get("title", "YouTube Video"),
        "thumb":    d.get("thumbnail"),
        "author":   d.get("author"),
        "duration": None, "views": None,
        "platform": "youtube", "formats": formats,
    }


# ── Facebook via ojoas API ─────────────────────────────────────────────────
def scrape_facebook(url):
    r = S.get(f"{OJOAS}/fb?url={urllib.parse.quote(url)}", timeout=20)
    if not r.ok:
        raise ValueError(f"Facebook API error: {r.status_code}")
    d = r.json()
    if not d.get("status"):
        raise ValueError(d.get("message", "Facebook fetch failed"))

    formats = []
    if d.get("HD"):
        formats.append({"label": "MP4 HD", "ext": "mp4",
                         "url": d["HD"], "sub": "High quality"})
    if d.get("Normal_video"):
        formats.append({"label": "MP4 SD", "ext": "mp4",
                         "url": d["Normal_video"], "sub": "Standard quality"})

    if not formats:
        raise ValueError("Facebook: No download links found")

    return {
        "title": "Facebook Video", "thumb": None,
        "author": None, "duration": None, "views": None,
        "platform": "facebook", "formats": formats,
    }


# ── Instagram via ojoas API ────────────────────────────────────────────────
def scrape_instagram(url):
    r = S.get(f"{OJOAS}/ig?url={urllib.parse.quote(url)}", timeout=20)
    if not r.ok:
        raise ValueError(f"Instagram API error: {r.status_code}")
    d = r.json()
    if not d.get("status"):
        raise ValueError(d.get("message", "Instagram fetch failed"))

    results = d.get("result", [])
    formats = []
    for i, item in enumerate(results[:6]):
        furl = item.get("url","")
        if not furl: continue
        is_video = ".mp4" in furl or "video" in furl.lower()
        formats.append({
            "label": f"{'Video' if is_video else 'Image'} {i+1}" if len(results) > 1 else ('MP4 Video' if is_video else 'JPG Image'),
            "ext":   "mp4" if is_video else "jpg",
            "url":   furl,
            "sub":   "MP4" if is_video else "JPG",
        })

    if not formats:
        raise ValueError("Instagram: No media found — post may be private")

    return {
        "title": "Instagram Media", "thumb": formats[0]["url"] if formats and formats[0]["ext"]=="jpg" else None,
        "author": None, "duration": None, "views": None,
        "platform": "instagram", "formats": formats,
    }


# ── Twitter via ojoas API ──────────────────────────────────────────────────
def scrape_twitter(url):
    r = S.get(f"{OJOAS}/twitter?url={urllib.parse.quote(url)}", timeout=20)
    if not r.ok:
        raise ValueError(f"Twitter API error: {r.status_code}")
    d = r.json()
    if not d.get("status"):
        raise ValueError(d.get("message", "Twitter fetch failed"))

    formats = []
    for item in (d.get("url") or []):
        if item.get("hd"):
            formats.append({"label": "MP4 HD", "ext": "mp4",
                             "url": item["hd"], "sub": "1080p"})
        if item.get("sd"):
            formats.append({"label": "MP4 SD", "ext": "mp4",
                             "url": item["sd"], "sub": "720p"})

    if not formats:
        raise ValueError("Twitter: No video found")

    return {
        "title": d.get("title","Twitter Video"), "thumb": None,
        "author": None, "duration": None, "views": None,
        "platform": "twitter", "formats": formats,
    }


# ── Routes ─────────────────────────────────────────────────────────────────
class InfoRequest(BaseModel):
    url: str

@app.post("/api/info")
async def get_info(req: InfoRequest):
    url = req.url.strip()
    platform = detect_platform(url)
    if platform == "unknown":
        raise HTTPException(422, "YouTube, TikTok, Instagram, Facebook හෝ Twitter link paste කරන්න")
    try:
        if platform == "tiktok":    return scrape_tiktok(url)
        if platform == "youtube":   return scrape_youtube(url)
        if platform == "facebook":  return scrape_facebook(url)
        if platform == "instagram": return scrape_instagram(url)
        if platform == "twitter":   return scrape_twitter(url)
    except Exception as e:
        raise HTTPException(422, str(e)[:300])


class StreamRequest(BaseModel):
    url: str
    ext: str = "mp4"
    filename: str = "saveit_video"

@app.post("/api/stream")
async def stream_file(req: StreamRequest):
    try:
        headers = {"Referer": "https://www.tikwm.com/", "User-Agent": S.headers["User-Agent"]}
        r = requests.get(req.url, stream=True, timeout=60, headers=headers)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", f"video/{req.ext}")
        size = r.headers.get("Content-Length")

        def gen():
            for chunk in r.iter_content(65536):
                yield chunk

        hdrs = {"Content-Disposition": f'attachment; filename="{req.filename}.{req.ext}"'}
        if size: hdrs["Content-Length"] = size
        return StreamingResponse(gen(), media_type=ct, headers=hdrs)
    except Exception as e:
        raise HTTPException(500, str(e)[:200])


@app.get("/api/health")
async def health():
    return {"status": "ok", "sources": ["tikwm", "ojoas-api"]}

@app.get("/")
async def root():
    index = os.path.join(static_dir, "index.html")
    return FileResponse(index) if os.path.exists(index) else {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
