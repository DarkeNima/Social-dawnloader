

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
import yt_dlp
import re, os, tempfile, time

app = FastAPI(docs_url=None, redoc_url=None)  # hide /docs in production

# ── CORS — allow only your own domain in production ────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Change to ["https://yourdomain.com"] in production
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Serve frontend ─────────────────────────────────────────────────────────
import os

# ── Serve frontend ─────────────────────────────────────────────────────────
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    index = os.path.join(static_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"status": "SaveIt API running — place index.html in /static/"}

DOWNLOAD_DIR = tempfile.mkdtemp()

# ── Session with rotating User-Agent ──────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
})

# ── Direct request helpers (no proxy needed — we ARE the server!) ──────────
def proxy_get(url, **kwargs):
    """Direct GET — backend has no CORS restrictions."""
    r = SESSION.get(url, timeout=15, **kwargs)
    if not r.ok:
        raise RuntimeError(f"Request failed: HTTP {r.status_code} → {url}")
    return r

def proxy_post(url, data, **kwargs):
    """Direct POST — backend has no CORS restrictions."""
    r = SESSION.post(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20, **kwargs,
    )
    if not r.ok:
        raise RuntimeError(f"Request failed: HTTP {r.status_code} → {url}")
    return r


# ── Platform detection ─────────────────────────────────────────────────────
def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:  return "youtube"
    if "tiktok.com"  in u or "vm.tiktok" in u: return "tiktok"
    if "instagram.com" in u:                    return "instagram"
    if "facebook.com" in u or "fb.watch" in u:  return "facebook"
    return "unknown"


# ── Scrapers (all hidden server-side) ─────────────────────────────────────

def scrape_tiktok(url: str) -> dict:
    home    = proxy_get("https://ssstik.io/en")
    match   = re.search(r'name="tt"\s+value="([^"]+)"', home.text)
    if not match:
        raise ValueError("ssstik token not found")
    token   = match.group(1)

    resp = proxy_post(
        "https://ssstik.io/abc?url=dl",
        data={"id": url, "locale": "en", "tt": token},
    )
    soup    = BeautifulSoup(resp.text, "html.parser")
    thumb   = soup.find("img", {"class": re.compile(r"result_image|mainpicture", re.I)})
    title_t = soup.find(class_=re.compile(r"maintext|result_author", re.I))

    formats, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"): continue
        txt  = a.get_text(strip=True).lower()

        if "watermark" in txt and ("no" in txt or "without" in txt): label = "MP4 No Watermark"
        elif "hd" in txt:        label = "MP4 HD"
        elif "watermark" in txt: label = "MP4 With Watermark"
        elif "mp3" in txt or "audio" in txt: label = "MP3 Audio"
        elif "mp4" in txt or "video" in txt: label = "MP4 Video"
        else:
            if not any(c in href for c in ["tikcdn","tiktok","cdn","muscdn"]): continue
            label = f"MP4 {len(formats)+1}"

        if label in seen: continue
        seen.add(label)
        formats.append({"label": label, "ext": "mp3" if "mp3" in label.lower() else "mp4", "url": href})

    if not formats:
        raise ValueError("No download links found — video may be private")

    return {
        "title":    title_t.get_text(strip=True) if title_t else "TikTok Video",
        "thumb":    thumb["src"] if thumb else None,
        "platform": "tiktok",
        "formats":  formats,
    }


def scrape_instagram(url: str) -> dict:
    home       = proxy_get("https://snapinsta.app/")
    tok_match  = re.search(r'name="token"\s+value="([^"]+)"', home.text, re.I) \
              or re.search(r'"_token"\s*:\s*"([^"]+)"', home.text, re.I)
    token      = tok_match.group(1) if tok_match else ""

    resp = proxy_post(
        "https://snapinsta.app/action.php",
        data={"url": url, "token": token, "lang": "en"},
    )
    data   = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
    html   = data.get("data", "")
    soup   = BeautifulSoup(html, "html.parser")
    thumb  = soup.find("img")
    formats, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a.get("href","")
        if not href.startswith("http"): continue
        txt  = a.get_text(strip=True)
        if "1080" in txt or "HD" in txt:   label = "MP4 1080p HD"
        elif "720" in txt:                  label = "MP4 720p"
        elif "mp4" in txt.lower():          label = "MP4 Video"
        elif "jpg" in txt.lower():          label = "JPG Image"
        else:                               label = f"Download {len(formats)+1}"
        if label in seen: continue
        seen.add(label)
        formats.append({"label": label, "ext": "jpg" if "JPG" in label else "mp4", "url": href})

    if not formats:
        raise ValueError("No links found — post may be private")

    return {"title": "Instagram Video", "thumb": thumb["src"] if thumb else None,
            "platform": "instagram", "formats": formats}


def scrape_facebook(url: str) -> dict:
    resp  = proxy_post(
        "https://snapsave.app/action.php?lang=en",
        data={"url": url},
    )
    data   = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
    html   = data.get("data","")
    soup   = BeautifulSoup(html, "html.parser")
    thumb  = soup.find("img")
    formats, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a.get("href","")
        if not href.startswith("http") and not href.startswith("//"): continue
        txt  = a.get_text(strip=True).lower()
        if "hd" in txt or "1080" in txt:   label = "MP4 HD"
        elif "sd" in txt or "480" in txt:  label = "MP4 SD"
        elif "mp3" in txt:                 label = "MP3 Audio"
        else:                              label = f"Download {len(formats)+1}"
        if label in seen: continue
        seen.add(label)
        full = "https:" + href if href.startswith("//") else href
        formats.append({"label": label, "ext": "mp3" if "MP3" in label else "mp4", "url": full})

    if not formats:
        raise ValueError("No links found — video may be private")

    return {"title": "Facebook Video", "thumb": thumb["src"] if thumb else None,
            "platform": "facebook", "formats": formats}


def scrape_youtube(url: str) -> dict:
    try:
        opts = {
            "quiet": True,
            "skip_download": True,
            "format": "bestvideo+bestaudio/best",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats, seen = [], set()
        for f in info.get("formats", []):
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            res    = f.get("height")
            fid    = f.get("format_id")
            furl   = f.get("url")

            if vcodec != "none" and res and furl:
                label = f"MP4 {res}p"
                if label not in seen:
                    seen.add(label)
                    formats.append({"label": label, "ext": "mp4",
                                    "url": furl, "format_id": fid})
            elif vcodec == "none" and acodec != "none" and furl:
                label = "MP3 Audio"
                if label not in seen:
                    seen.add(label)
                    formats.append({"label": label, "ext": "mp3",
                                    "url": furl, "format_id": fid})

        formats.sort(key=lambda x: (
            x["type"] == "audio" if "type" in x else False,
            -(int(re.sub(r"[^0-9]", "", x["label"]) or "0"))
        ))

        return {
            "title":    info.get("title", "YouTube Video"),
            "thumb":    info.get("thumbnail"),
            "author":   info.get("uploader"),
            "duration": str(int(info.get("duration", 0) or 0)) + "s",
            "platform": "youtube",
            "formats":  formats[:8],
        }
    except Exception as e:
        raise ValueError(str(e))


# ── API routes ─────────────────────────────────────────────────────────────

class InfoRequest(BaseModel):
    url: str

@app.post("/api/info")
async def get_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "No URL")
    platform = detect_platform(url)
    if platform == "unknown":
        raise HTTPException(422, "Unsupported platform")
    try:
        if platform == "tiktok":    return scrape_tiktok(url)
        if platform == "instagram": return scrape_instagram(url)
        if platform == "facebook":  return scrape_facebook(url)
        if platform == "youtube":   return scrape_youtube(url)
    except Exception as e:
        raise HTTPException(422, str(e))


class StreamRequest(BaseModel):
    url: str
    ext: str = "mp4"
    filename: str = "saveit_video"

@app.post("/api/stream")
async def stream_file(req: StreamRequest):
    """Proxy-stream a direct video URL to browser."""
    try:
        r = SESSION.get(req.url, stream=True, timeout=30,
                        headers={"Referer": "https://ssstik.io"})
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
    return {"status": "ok", "mode": "direct"}

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")

# ── Run ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))  # Railway sets PORT automatically
    uvicorn.run("main:app", host="0.0.0.0", port=port)
