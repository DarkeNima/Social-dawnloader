
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests, os, re, tempfile, random

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

DOWNLOAD_DIR = tempfile.mkdtemp()

# ── Cobalt public instances (no auth, no key needed) ──────────────────────
# These are public community instances from instances.cobalt.best
COBALT_INSTANCES = [
    "https://cobalt.synzr.space",
    "https://cobalt.privacyredirect.com",
    "https://cobalt.zt-tech.eu",
    "https://co.wuk.sh",
    "https://cobalt.vuiis.eu",
    "https://dl.lao.sb",
]

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "SaveIt/1.0 (+https://github.com/saveit)",
}

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "tiktok.com" in u or "vm.tiktok" in u or "vt.tiktok" in u: return "tiktok"
    if "instagram.com" in u: return "instagram"
    if "facebook.com" in u or "fb.watch" in u: return "facebook"
    if "twitter.com" in u or "x.com" in u: return "twitter"
    if "reddit.com" in u: return "reddit"
    return "other"

def expand_url(url: str) -> str:
    try:
        r = requests.head(url, allow_redirects=True, timeout=8,
            headers={"User-Agent": "Mozilla/5.0"})
        return r.url
    except:
        return url

def cobalt_request(url: str, quality: str = "1080", audio_only: bool = False) -> dict:
    """Try each cobalt instance until one works."""
    instances = COBALT_INSTANCES.copy()
    random.shuffle(instances)  # load balance

    payload = {
        "url": url,
        "videoQuality": quality,
        "audioFormat": "mp3",
        "filenameStyle": "pretty",
        "downloadMode": "audio" if audio_only else "auto",
    }

    last_error = "All instances failed"
    for instance in instances:
        try:
            r = requests.post(
                f"{instance}/",
                json=payload,
                headers=HEADERS,
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                status = data.get("status", "")
                if status in ("stream", "redirect", "tunnel"):
                    return {"ok": True, "url": data.get("url"), "instance": instance, "status": status}
                elif status == "picker":
                    # Multiple items (e.g. Instagram carousel)
                    items = data.get("picker", [])
                    urls = [{"url": i.get("url"), "type": i.get("type","video")} for i in items if i.get("url")]
                    if urls:
                        return {"ok": True, "picker": urls, "instance": instance}
                elif status == "error":
                    last_error = data.get("error", {}).get("code", "unknown error")
                    continue
            else:
                last_error = f"HTTP {r.status_code}"
        except requests.exceptions.Timeout:
            last_error = "timeout"
        except Exception as e:
            last_error = str(e)[:100]
    
    return {"ok": False, "error": last_error}


def scrape_info(url: str, platform: str) -> dict:
    """Get video info + all quality options."""
    # Expand short URLs
    if any(x in url for x in ["vt.tiktok", "vm.tiktok", "fb.watch"]):
        url = expand_url(url)

    formats = []
    title = "Video"
    thumb = None

    # Try multiple qualities
    qualities = ["1080", "720", "480", "360"]
    first_result = None

    for quality in qualities:
        result = cobalt_request(url, quality=quality)
        if result["ok"]:
            if first_result is None:
                first_result = result

            if result.get("picker"):
                # Carousel / multiple items
                for i, item in enumerate(result["picker"][:4]):
                    ext = "mp3" if item["type"] == "audio" else "mp4"
                    formats.append({
                        "label": f"Item {i+1} {ext.upper()}",
                        "ext": ext,
                        "url": item["url"],
                        "sub": "",
                    })
                break
            else:
                furl = result.get("url")
                if furl:
                    label = f"MP4 {quality}p"
                    if not any(f["label"] == label for f in formats):
                        formats.append({
                            "label": label,
                            "ext": "mp4",
                            "url": furl,
                            "sub": quality + "p",
                        })

    # Add audio option
    audio_result = cobalt_request(url, audio_only=True)
    if audio_result["ok"] and audio_result.get("url"):
        formats.append({
            "label": "MP3 Audio",
            "ext": "mp3",
            "url": audio_result["url"],
            "sub": "audio only",
        })

    if not formats:
        error = first_result.get("error", "Could not fetch video") if first_result else "All instances unavailable"
        raise ValueError(error)

    # Try get thumbnail from YouTube oEmbed
    if platform == "youtube":
        try:
            oe = requests.get(
                f"https://www.youtube.com/oembed?url={requests.utils.quote(url)}&format=json",
                timeout=5
            )
            if oe.ok:
                info = oe.json()
                title = info.get("title", title)
                thumb = info.get("thumbnail_url")
        except: pass

    return {
        "title": title,
        "thumb": thumb,
        "author": None,
        "duration": None,
        "views": None,
        "platform": platform,
        "formats": formats[:6],
    }


# ── Routes ─────────────────────────────────────────────────────────────────
class InfoRequest(BaseModel):
    url: str

@app.post("/api/info")
async def get_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL නෑ")
    platform = detect_platform(url)
    try:
        return scrape_info(url, platform)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, str(e)[:200])


class StreamRequest(BaseModel):
    url: str
    ext: str = "mp4"
    filename: str = "saveit_video"

@app.post("/api/stream")
async def stream_file(req: StreamRequest):
    """Proxy stream to browser."""
    try:
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        r = s.get(req.url, stream=True, timeout=60)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", f"video/{req.ext}")
        size = r.headers.get("Content-Length")

        def gen():
            for chunk in r.iter_content(65536):
                yield chunk

        headers = {"Content-Disposition": f'attachment; filename="{req.filename}.{req.ext}"'}
        if size:
            headers["Content-Length"] = size

        return StreamingResponse(gen(), media_type=ct, headers=headers)
    except Exception as e:
        raise HTTPException(500, str(e)[:200])


@app.get("/api/health")
async def health():
    return {"status": "ok", "engine": "cobalt", "instances": len(COBALT_INSTANCES)}

@app.get("/")
async def root():
    index = os.path.join(static_dir, "index.html")
    return FileResponse(index) if os.path.exists(index) else {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
