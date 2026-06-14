import os
import json
import time
import threading
import queue
import subprocess
import sys
import shutil
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Global state
download_queue = queue.Queue()
download_progress = {}  # video_id -> progress info
download_lock = threading.Lock()
active_downloads = {}  # video_id -> thread
sse_clients = []  # list of queues for SSE clients
sse_lock = threading.Lock()

DEFAULT_DOWNLOAD_DIR = str(Path.home() / "Downloads")

# ── ffmpeg detection ─────────────────────────────────────────────────────────
def detect_ffmpeg():
    """Return the ffmpeg executable path, or None if not found."""
    # 1. Check PATH
    found = shutil.which("ffmpeg")
    if found:
        return found
    # 2. Check common Windows locations (including winget install paths)
    winget_base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    winget_ffmpegs = sorted(winget_base.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe")) if winget_base.exists() else []
    common = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        str(Path.home() / "ffmpeg" / "bin" / "ffmpeg.exe"),
    ] + [str(p) for p in winget_ffmpegs]
    for p in common:
        if os.path.isfile(p):
            return p
    return None

FFMPEG_PATH = detect_ffmpeg()
HAS_FFMPEG = FFMPEG_PATH is not None
print(f"ffmpeg: {'found at ' + FFMPEG_PATH if HAS_FFMPEG else 'NOT FOUND — will use single-file fallback formats'}")


def broadcast_event(event_type, data):
    """Send an event to all connected SSE clients."""
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait({"type": event_type, "data": data})
            except Exception:
                dead.append(q)
        for d in dead:
            sse_clients.remove(d)


def yt_dlp_progress_hook(video_id):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            percent = (downloaded / total * 100) if total > 0 else 0
            speed_str = format_speed(speed)
            eta_str = format_eta(eta)

            with download_lock:
                if video_id in download_progress:
                    download_progress[video_id].update({
                        "status": "downloading",
                        "percent": round(percent, 1),
                        "speed": speed_str,
                        "eta": eta_str,
                        "downloaded": format_bytes(downloaded),
                        "total": format_bytes(total),
                    })
            broadcast_event("progress", {
                "video_id": video_id,
                "percent": round(percent, 1),
                "speed": speed_str,
                "eta": eta_str,
                "downloaded": format_bytes(downloaded),
                "total": format_bytes(total),
                "status": "downloading"
            })

        elif d["status"] == "finished":
            with download_lock:
                if video_id in download_progress:
                    download_progress[video_id]["status"] = "processing"
            broadcast_event("progress", {
                "video_id": video_id,
                "status": "processing",
                "percent": 100,
            })

        elif d["status"] == "error":
            with download_lock:
                if video_id in download_progress:
                    download_progress[video_id]["status"] = "error"
            broadcast_event("progress", {
                "video_id": video_id,
                "status": "error",
            })

    return hook


def format_speed(bps):
    if not bps:
        return "—"
    if bps > 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    if bps > 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps:.0f} B/s"


def format_bytes(b):
    if not b:
        return "—"
    if b > 1024 * 1024 * 1024:
        return f"{b / 1024 / 1024 / 1024:.2f} GB"
    if b > 1024 * 1024:
        return f"{b / 1024 / 1024:.1f} MB"
    if b > 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"


def format_eta(seconds):
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def get_format_string(quality, fmt):
    """Build yt-dlp format string based on quality and format.
    
    When ffmpeg is available, use bestvideo+bestaudio (separate streams, merged).
    When ffmpeg is NOT available, use single pre-merged file formats to avoid the
    'ffmpeg not installed' error — these top out around 720p on YouTube.
    """
    if fmt == "mp3":
        return "bestaudio/best"

    if HAS_FFMPEG:
        # Full quality — separate video+audio merged by ffmpeg
        quality_map = {
            "best":  "bestvideo+bestaudio/best",
            "4k":    "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
            "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        }
    else:
        # No ffmpeg — only request pre-merged MP4 streams (YouTube caps these ~720p)
        quality_map = {
            "best":  "best[ext=mp4]/best",
            "4k":    "best[ext=mp4][height<=2160]/best[height<=2160]/best",
            "1080p": "best[ext=mp4][height<=1080]/best[height<=1080]/best",
            "720p":  "best[ext=mp4][height<=720]/best[height<=720]/best",
            "480p":  "best[ext=mp4][height<=480]/best[height<=480]/best",
            "360p":  "best[ext=mp4][height<=360]/best[height<=360]/best",
        }
    return quality_map.get(quality, quality_map["best"])


def get_base_ydl_opts(video_id, output_dir, format_str, player_client="tv_embedded"):
    """Build base yt-dlp options with anti-403 headers and player client spoofing."""
    opts = {
        "format": format_str,
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "progress_hooks": [yt_dlp_progress_hook(video_id)],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        # --- Anti-403 settings ---
        # Use a specific player client that YouTube hasn't blocked
        "extractor_args": {
            "youtube": {
                "player_client": [player_client],
                "player_skip": ["webpage", "configs"],
            }
        },
        # Mimic a real browser
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        # Retry on transient errors
        "retries": 5,
        "fragment_retries": 5,
        "retry_sleep_functions": {"http": lambda n: 2 ** n},
        "socket_timeout": 30,
        "js_runtimes": {
            "node": {}
        },
        "remote_components": ["ejs:github"],
    }

    if HAS_FFMPEG:
        opts["ffmpeg_location"] = os.path.dirname(FFMPEG_PATH)

    return opts


def try_get_cookies():
    """Try to find browser cookies — returns (cookiesfrombrowser tuple) or None."""
    for browser in ("edge", "chrome", "firefox", "brave", "opera"):
        try:
            import yt_dlp
            # Quick probe — does the browser exist on this machine?
            test_opts = {"quiet": True, "no_warnings": True, "cookiesfrombrowser": (browser,)}
            with yt_dlp.YoutubeDL(test_opts) as ydl:
                ydl.cookiejar  # just access it; will raise if browser not found
            return (browser,)
        except Exception:
            continue
    return None


def download_video_worker(video_id, url, quality, fmt, output_dir, cookies_source="none"):
    """Worker function to download a single video.
    
    Tries multiple player clients to beat 403 Forbidden errors from YouTube.
    Order: tv_embedded → android → web → mweb (last resort with cookies).
    """
    import yt_dlp

    format_str = get_format_string(quality, fmt)
    print(f"[Worker] Thread started: video_id={video_id}, url={url}, quality={quality}, format={fmt}, cookies={cookies_source}, dir={output_dir}")

    # Player clients to try in order — tv_embedded is most reliable for bypassing 403
    PLAYER_CLIENTS = ["tv_embedded", "android", "web", "mweb"]

    mp3_postprocessors = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }] if (fmt == "mp3" and HAS_FFMPEG) else []

    last_error = None

    for i, client in enumerate(PLAYER_CLIENTS):
        try:
            opts = get_base_ydl_opts(video_id, output_dir, format_str, player_client=client)

            if HAS_FFMPEG and fmt != "mp3":
                opts["merge_output_format"] = "mp4"
            if mp3_postprocessors:
                opts["postprocessors"] = mp3_postprocessors

            # Apply user-selected cookies, or fallback to auto-detected cookies on last try
            if cookies_source and cookies_source != "none":
                opts["cookiesfrombrowser"] = (cookies_source,)
            elif i == len(PLAYER_CLIENTS) - 1:
                cookies = try_get_cookies()
                if cookies:
                    opts["cookiesfrombrowser"] = cookies

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            # Success
            with download_lock:
                if video_id in download_progress:
                    download_progress[video_id]["status"] = "done"
            broadcast_event("progress", {"video_id": video_id, "status": "done", "percent": 100})
            return  # done — exit retry loop

        except Exception as e:
            last_error = str(e)
            err_lower = last_error.lower()
            # Only retry on access-denied type errors
            if any(kw in err_lower for kw in ("403", "forbidden", "http error 4", "sign in", "not available")):
                continue
            # Any other error (network down, etc.) — stop immediately
            break

    # All attempts failed
    print(f"[Worker] Download failed for {video_id}. Error: {last_error}")
    with download_lock:
        if video_id in download_progress:
            download_progress[video_id]["status"] = "error"
            download_progress[video_id]["error"] = last_error or "Unknown error"
    broadcast_event("progress", {
        "video_id": video_id,
        "status": "error",
        "error": last_error or "Unknown error",
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/fetch-playlist", methods=["POST"])
def fetch_playlist():
    """Fetch playlist/video metadata."""
    import yt_dlp

    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "js_runtimes": {
            "node": {}
        },
        "remote_components": ["ejs:github"],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        videos = []
        if "entries" in info:
            # Playlist
            for entry in info["entries"]:
                if entry is None:
                    continue
                video_id = entry.get("id", "")
                videos.append({
                    "id": video_id,
                    "title": entry.get("title", "Unknown"),
                    "url": entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                    "duration": format_duration(entry.get("duration")),
                    "thumbnail": entry.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                    "channel": entry.get("channel") or entry.get("uploader", ""),
                    "view_count": format_views(entry.get("view_count")),
                })
        else:
            # Single video
            video_id = info.get("id", "")
            videos.append({
                "id": video_id,
                "title": info.get("title", "Unknown"),
                "url": info.get("webpage_url") or url,
                "duration": format_duration(info.get("duration")),
                "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                "channel": info.get("channel") or info.get("uploader", ""),
                "view_count": format_views(info.get("view_count")),
            })

        return jsonify({
            "title": info.get("title", "Playlist"),
            "count": len(videos),
            "videos": videos,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def format_duration(seconds):
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_views(count):
    if not count:
        return ""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M views"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K views"
    return f"{count} views"


@app.route("/api/download", methods=["POST"])
def start_download():
    """Start downloading selected videos."""
    data = request.json
    videos = data.get("videos", [])
    quality = data.get("quality", "best")
    fmt = data.get("format", "mp4")
    output_dir = data.get("output_dir", DEFAULT_DOWNLOAD_DIR)
    cookies_source = data.get("cookies_source", "none")

    print(f"[API] Start download: count={len(videos)}, quality={quality}, format={fmt}, cookies={cookies_source}, output_dir={output_dir}")

    # Create output dir if not exists
    os.makedirs(output_dir, exist_ok=True)

    started = []
    for video in videos:
        video_id = video["id"]
        url = video["url"]
        
        with download_lock:
            download_progress[video_id] = {
                "status": "pending",
                "title": video.get("title", "Unknown"),
                "percent": 0,
                "speed": "—",
                "eta": "—",
            }

        t = threading.Thread(
            target=download_video_worker,
            args=(video_id, url, quality, fmt, output_dir, cookies_source),
            daemon=True
        )
        t.start()
        active_downloads[video_id] = t
        started.append(video_id)

    return jsonify({"started": started})


@app.route("/api/progress")
def progress_stream():
    """SSE endpoint for real-time progress updates."""
    client_queue = queue.Queue()
    with sse_lock:
        sse_clients.append(client_queue)

    # Send current state immediately
    with download_lock:
        current = dict(download_progress)

    def generate():
        # Send current state
        for vid_id, info in current.items():
            data = {"video_id": vid_id, **info}
            yield f"data: {json.dumps({'type': 'progress', 'data': data})}\n\n"

        while True:
            try:
                event = client_queue.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
            except GeneratorExit:
                break

        with sse_lock:
            if client_queue in sse_clients:
                sse_clients.remove(client_queue)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    """Open a folder in Explorer."""
    data = request.json
    folder = data.get("folder", DEFAULT_DOWNLOAD_DIR)
    try:
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.run(["open", folder])
        else:
            subprocess.run(["xdg-open", folder])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/browse-folder", methods=["GET"])
def browse_folder():
    """Open a folder picker dialog (Windows) using a subprocess to avoid tkinter thread crashes, and return selected path."""
    try:
        if sys.platform == "win32":
            # Run tkinter dialog in a separate python process so it doesn't block or crash Flask's thread
            code = (
                "import tkinter as tk; "
                "from tkinter import filedialog; "
                "root = tk.Tk(); "
                "root.withdraw(); "
                "root.wm_attributes('-topmost', 1); "
                f"folder = filedialog.askdirectory(initialdir={repr(DEFAULT_DOWNLOAD_DIR)}); "
                "print(folder); "
                "root.destroy()"
            )
            res = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=True
            )
            folder = res.stdout.strip()
            if folder and os.path.isdir(folder):
                return jsonify({"folder": folder})
        return jsonify({"folder": DEFAULT_DOWNLOAD_DIR})
    except Exception as e:
        print(f"Error browsing folder: {e}")
        return jsonify({"folder": DEFAULT_DOWNLOAD_DIR, "error": str(e)})


@app.route("/api/quick-dir")
def get_quick_dir():
    """Resolve and return standard Windows directories for quick selection."""
    folder_type = request.args.get("type", "Downloads")
    home = Path.home()
    if folder_type == "Desktop":
        path = home / "Desktop"
    elif folder_type == "Music":
        path = home / "Music"
    elif folder_type == "Videos":
        path = home / "Videos"
    else:
        path = home / "Downloads"
    
    # Fallback to home if folder doesn't exist
    if not path.exists():
        path = home
    
    return jsonify({"dir": str(path)})


@app.route("/api/cancel", methods=["POST"])
def cancel_download():
    """Mark a download as cancelled (best-effort)."""
    data = request.json
    video_id = data.get("video_id", "")
    with download_lock:
        if video_id in download_progress:
            download_progress[video_id]["status"] = "cancelled"
    broadcast_event("progress", {"video_id": video_id, "status": "cancelled"})
    return jsonify({"ok": True})


@app.route("/api/clear-completed", methods=["POST"])
def clear_completed():
    """Remove completed/failed entries from progress."""
    with download_lock:
        to_remove = [vid for vid, info in download_progress.items()
                     if info["status"] in ("done", "error", "cancelled")]
        for vid in to_remove:
            del download_progress[vid]
    return jsonify({"cleared": len(to_remove)})


@app.route("/api/default-dir")
def get_default_dir():
    return jsonify({"dir": DEFAULT_DOWNLOAD_DIR})


@app.route("/api/system-info")
def system_info():
    """Return system capabilities for the frontend."""
    return jsonify({
        "ffmpeg": HAS_FFMPEG,
        "ffmpeg_path": FFMPEG_PATH,
        "platform": sys.platform,
    })


@app.after_request
def add_header(r):
    """Add headers to prevent caching of static files."""
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r


if __name__ == "__main__":
    print("YouTube Playlist Downloader")
    print("=" * 40)
    print("Open your browser at: http://localhost:5000")
    print("=" * 40)
    app.run(debug=False, threaded=True, port=5000)
