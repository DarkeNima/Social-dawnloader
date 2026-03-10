"""
SaveIt – FastAPI Backend
All proxy/scraping logic is server-side. Frontend only sees clean JSON.

Install:
    pip install fastapi uvicorn requests beautifulsoup4 yt-dlp

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

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
app.mount("/static", StaticFiles(directory="static"), name="static")

DOWNLOAD_DIR = tempfile.mkdtemp()

# ── Session with rotating User-Agent ──────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
})

# ── Internal proxy fallback chain (hidden from frontend!) ──────────────────
PROXIES = [
    lambda u: f"https://corsproxy.io/?url={requests.utils.quote(u, safe='')}",
    lambda u: f"https://api.allorigins.win/raw?url={requests.utils.quote(u, safe='')}",
    lambda u: f"https://thingproxy.freeboard.io/fetch/{u}",
]

def proxy_get(url, **kwargs):
    """Try each proxy in order. Raise on all failures."""
    last_err = ""
    for make_proxy in PROXIES:
        try:
            r = SESSION.get(make_proxy(url), timeout=12, **kwargs)
            if r.status_code in (429, 403, 407) or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}"
                continue
            if r.ok:
                return r
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"All proxies failed: {last_err}")

def proxy_post(url, data, **kwargs):
    last_err = ""
    for make_proxy in PROXIES:
        try:
            r = SESSION.post(
                make_proxy(url), data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15, **kwargs,
            )
            if r.status_code in (429, 403, 407) or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}"
                continue
            if r.ok:
                return r
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"All proxies failed: {last_err}")


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
    title, thumb, author, duration = "YouTube Video", None, None, None
    try:
        oe = SESSION.get(
            f"https://www.youtube.com/oembed?url={requests.utils.quote(url)}&format=json",
            timeout=8,
        )
        if oe.ok:
            info   = oe.json()
            title  = info.get("title", title)
            thumb  = info.get("thumbnail_url")
            author = info.get("author_name")
    except: pass

    formats = []
    try:
        r = proxy_post(
            "https://www.y2mate.com/mates/analyzeV2/ajax",
            data={"k_query": url, "k_page": "home", "hl": "en", "q_auto": "0"},
        )
        d = r.json()
        if d.get("t"): duration = d["t"]
        for q in ["1080p","720p","480p","360p"]:
            key = next((k for k in (d.get("links",{}).get("mp4") or {}) if q.replace("p","") in k), None)
            if key:
                formats.append({"label": f"MP4 {q}", "ext": "mp4",
                                 "url": None, "k": d["links"]["mp4"][key].get("k"), "vid": d.get("vid")})
        mp3s = d.get("links",{}).get("mp3") or {}
        k128 = next((k for k in mp3s if "128" in k), None)
        if k128:
            formats.append({"label": "MP3 Audio", "ext": "mp3",
                             "url": None, "k": mp3s[k128].get("k"), "vid": d.get("vid")})
    except: pass

    if not formats:
        formats.append({"label":"Open Y2Mate","ext":"mp4",
                         "url": f"https://www.y2mate.com/youtube/{url}", "external": True})

    return {"title": title, "thumb": thumb, "author": author,
            "duration": duration, "platform": "youtube", "formats": formats}


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


class ConvertRequest(BaseModel):
    vid: str
    k:   str

@app.post("/api/yt-convert")
async def yt_convert(req: ConvertRequest):
    """YouTube y2mate convert step — hidden from frontend."""
    try:
        r = proxy_post(
            "https://www.y2mate.com/mates/convertV2/index",
            data={"vid": req.vid, "k": req.k},
        )
        d = r.json()
        if d.get("dlink"):
            return {"url": d["dlink"]}
        raise HTTPException(422, "Convert failed")
    except Exception as e:
        raise HTTPException(500, str(e))


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
    return {"status": "ok", "proxies": len(PROXIES)}
