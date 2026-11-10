#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MP3 音樂下載器（本地網頁版）
流程：輸入歌手名 → 列出所有歌曲/專輯 → 勾選 → 逐首到 YouTube 搜尋下載 → 統一轉成 MP3
資料來源：iTunes Search API（歌單列表） + yt-dlp（YouTube 下載）
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
TMP_DIR = BASE_DIR / ".tmp"
DOWNLOADS_SUBDIR = "下載"
PORT_START = 8765

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MP3Downloader/1.0"}


def log(*args):
    """安全列印：在 pythonw（無控制台）環境下 stdout 為 None，不能直接 print"""
    try:
        print(*args, flush=True)
    except Exception:
        pass


# ---------------- 設定 ----------------
def load_config():
    cfg = {"output_dir": str(BASE_DIR), "country": "TW", "quality": "320"}
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


CONFIG = load_config()


def save_config():
    try:
        CONFIG_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def find_ffmpeg():
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg") or ""


FFMPEG = find_ffmpeg()


def check_deps():
    missing = []
    for mod, pkg in (("yt_dlp", "yt-dlp"), ("mutagen", "mutagen")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        log("缺少依賴，正在自動安裝：%s" % ", ".join(missing))
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", *missing, "imageio-ffmpeg"])
    if not FFMPEG:
        log("[警告] 找不到 ffmpeg，轉 MP3 可能會失敗。")


# ---------------- iTunes 歌單資料 ----------------
def itunes_get(url):
    req = urllib.request.Request(url, headers=UA)
    last_err = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise RuntimeError("iTunes API 請求失敗：%s" % last_err)


def search_artists(q, limit=12):
    url = "https://itunes.apple.com/search?term=%s&entity=musicArtist&limit=%d" % (
        urllib.parse.quote(q), limit)
    data = itunes_get(url)
    out = []
    for a in data.get("results", []):
        out.append({
            "id": a.get("artistId"),
            "name": a.get("artistName", ""),
            "genre": a.get("primaryGenreName", ""),
        })
    return out


COUNTRIES_POOL = ["TW", "JP", "US", "HK", "KR"]


def _fetch_songs_country(artist_id, country):
    url = "https://itunes.apple.com/lookup?id=%s&entity=song&limit=200&country=%s" % (
        artist_id, urllib.parse.quote(country))
    try:
        data = itunes_get(url)
        return [t for t in data.get("results", [])
                if t.get("wrapperType") == "track" and t.get("kind") == "song"]
    except Exception:
        return []


def _norm_name(name):
    """把專輯／歌名標準化（去掉空白與標點、轉小寫），用來比對是否同一張專輯。"""
    return re.sub(r"\W+", "", str(name).lower())


def _script_counts(title):
    kana = hangul = hanzi = latin = 0
    for ch in str(title):
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:            # 平假名＋片假名
            kana += 1
        elif 0xAC00 <= o <= 0xD7AF:          # 韓文音節
            hangul += 1
        elif 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF:
            hanzi += 1                       # 漢字
        elif "a" <= ch.lower() <= "z":
            latin += 1
    return kana, hangul, hanzi, latin


def _detect_lang(titles):
    """由全部歌名推斷藝人語言：有假名 → 日文；有諺文 → 韓文；只有漢字 → 中文。"""
    kana = hangul = hanzi = 0
    for t in titles:
        k, h, c, _ = _script_counts(t)
        kana += k
        hangul += h
        hanzi += c
    if kana > hangul and kana > 0:
        return "ja"
    if hangul > 0:
        return "ko"
    if hanzi > 0:
        return "zh"
    return "en"


def _lang_score(title, lang):
    """歌名與藝人語言的匹配分數，分數高的優先保留。"""
    kana, hangul, hanzi, latin = _script_counts(title)
    if lang == "ja":
        if kana:
            return 30          # 有假名 → 肯定日文
        if hanzi:
            return 20          # 純漢字（日文標題常用漢字）
        return 10 if latin else 0
    if lang == "ko":
        if hangul:
            return 30
        return 10 if latin else 0
    if lang == "zh":
        if hanzi and not kana:
            return 30
        return 10 if latin else 0
    # en / 其他：純英文優先
    if latin and not (kana or hangul or hanzi):
        return 30
    return 10


def artist_albums_songs(artist_id, country):
    """回傳該歌手的專輯與歌曲（按專輯分組）。

    單一地區商店最多只回傳 200 首，所以這裡同時查多個地區商店，
    把結果合併去重，盡量湊齊完整歌單。
    """
    from concurrent.futures import ThreadPoolExecutor
    primary = (country or "TW").upper()
    countries = [primary] + [c for c in COUNTRIES_POOL if c != primary]
    with ThreadPoolExecutor(max_workers=5) as ex:
        per_country = list(ex.map(lambda c: _fetch_songs_country(artist_id, c), countries))

    # 不同地區商店會把同一張專輯的歌名/專輯名翻譯成不同語言，
    # 所以合併規則：專輯名相同 → 同一張專輯；專輯內用「時長」對齊去重；
    # 重複歌曲的歌名，按藝人語言優先保留（日本藝人 → 日文名優先）。
    tracks_all = []
    countries_used = []
    for c, tracks in zip(countries, per_country):
        if tracks:
            countries_used.append(c)
        for t in tracks:
            tracks_all.append(t)

    artist_name = ""
    lang = _detect_lang([t.get("trackName", "") for t in tracks_all])

    albums = {}
    order = []
    for idx, t in enumerate(tracks_all):
        if not artist_name:
            artist_name = t.get("artistName", "")
        aname = t.get("collectionName") or "未收錄專輯"
        akey = _norm_name(aname)
        if akey not in albums:
            albums[akey] = {"names": [], "tracks": {}, "titles": {}, "years": [],
                            "cid": t.get("collectionId") or 0}
            order.append(akey)
        g = albums[akey]
        if aname not in g["names"]:
            g["names"].append(aname)
        g["years"].append((t.get("releaseDate") or "")[:4])

        dur = t.get("trackTimeMillis") or 0
        tkey = int(dur) if dur else ("name:" + _norm_name(t.get("trackName", "")))
        tlist = g["titles"].setdefault(tkey, [])
        if t.get("trackName") and t.get("trackName") not in tlist:
            tlist.append(t.get("trackName"))
        cand = {
            "title": t.get("trackName", ""),
            "artist": t.get("artistName", ""),
            "duration": dur,
            "year": (t.get("releaseDate") or "")[:4],
            "no": t.get("trackNumber") or 999,
            "score": _lang_score(t.get("trackName", ""), lang),
            "order": idx,
        }
        rec = g["tracks"].get(tkey)
        if rec is None:
            g["tracks"][tkey] = cand
        else:
            cand["no"] = min(cand["no"], rec["no"])
            # 同一首歌（時長相同）：保留藝人語言優先的歌名
            if cand["score"] > rec["score"]:
                g["tracks"][tkey] = cand
            else:
                g["tracks"][tkey]["no"] = cand["no"]

    result = []
    for akey in order:
        g = albums[akey]
        title = max(g["names"], key=lambda n: _lang_score(n, lang))
        years = [y for y in g["years"] if y]
        items = sorted(g["tracks"].items(),
                       key=lambda kv: (kv[1]["no"], kv[1]["order"]))
        songs = []
        for tkey, s in items:
            alt = [x for x in g["titles"].get(tkey, []) if x != s["title"]]
            songs.append({"title": s["title"], "artist": s["artist"],
                          "duration": s["duration"], "year": s["year"],
                          "alt": alt})
        result.append({
            "id": g["cid"],
            "title": title,
            "year": min(years) if years else "",
            "songs": songs,
        })
    # 第二層合併：專輯名不同但曲目時長完全相同（例如同一首歌出過改名單曲）→ 當作同一專輯
    by_sig = {}
    final = []
    for a in result:
        if not a["songs"]:
            final.append(a)
            continue
        sig = tuple(sorted(s["duration"] for s in a["songs"]))
        base = by_sig.get(sig)
        if base is None:
            a["_names"] = [a["title"]]
            by_sig[sig] = a
            final.append(a)
            continue
        base["_names"].append(a["title"])
        if a["year"] and (not base["year"] or a["year"] < base["year"]):
            base["year"] = a["year"]
        other = {}
        for s in a["songs"]:
            other.setdefault(s["duration"], []).append(s)
        merged_songs = []
        for s in base["songs"]:
            lst = other.get(s["duration"])
            if lst:
                o = lst.pop(0)
                keep = o if _lang_score(o["title"], lang) > _lang_score(s["title"], lang) else s
                drop = s if keep is o else o
                allnames = [keep["title"]] + keep.get("alt", []) + [drop["title"]] + drop.get("alt", [])
                seen = set()
                keep["alt"] = [x for x in allnames
                               if x != keep["title"] and not (x in seen or seen.add(x))]
                merged_songs.append(keep)
            else:
                merged_songs.append(s)
        for v in other.values():
            merged_songs.extend(v)
        base["songs"] = merged_songs
    for a in final:
        names = a.pop("_names", None)
        if names:
            a["title"] = max(names, key=lambda n: _lang_score(n, lang))

    result = final
    result.sort(key=lambda a: a.get("year") or "9999")
    return {"artist": artist_name, "albums": result,
            "countries_used": countries_used, "lang": lang}


# ---------------- 下載佇列 ----------------
class StopDownload(Exception):
    pass


JOBS = []
JOBS_LOCK = threading.Lock()
STOP_FLAG = threading.Event()
WORKER = {"running": False}


def sanitize(name):
    name = re.sub(r'[<>:"/\\|?*]', "_", str(name)).strip()
    name = re.sub(r"\s+", " ", name).strip(". ")
    return name[:120] or "untitled"


def add_jobs(tracks):
    with JOBS_LOCK:
        for t in tracks:
            title = (t.get("title") or "").strip()
            if not title:
                continue
            JOBS.append({
                "id": uuid.uuid4().hex[:8],
                "title": title,
                "artist": (t.get("artist") or "").strip(),
                "album": (t.get("album") or "").strip(),
                "alt": [x.strip() for x in (t.get("alt") or []) if x and x.strip()],
                "status": "pending",
                "progress": 0,
                "message": "等待中",
                "file": "",
            })
    ensure_worker()


def ensure_worker():
    with JOBS_LOCK:
        if WORKER["running"]:
            return
        WORKER["running"] = True
    threading.Thread(target=worker_loop, daemon=True).start()


def worker_loop():
    from yt_dlp import YoutubeDL  # 延遲載入
    while True:
        if STOP_FLAG.is_set():
            with JOBS_LOCK:
                for j in JOBS:
                    if j["status"] in ("pending", "downloading"):
                        j["status"] = "stopped"
                        j["message"] = "已停止"
            STOP_FLAG.clear()
            break
        job = None
        with JOBS_LOCK:
            for j in JOBS:
                if j["status"] == "pending":
                    job = j
                    break
        if job is None:
            break
        try:
            download_one(job, YoutubeDL)
        except Exception as e:
            job["status"] = "error"
            job["message"] = str(e)[:200]
    with JOBS_LOCK:
        WORKER["running"] = False


def downloads_dir():
    d = Path(CONFIG.get("output_dir") or str(BASE_DIR)) / DOWNLOADS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def album_dir(album):
    # 每張專輯一個資料夾：下載 / <專輯名> /
    name = sanitize(album) if album else ""
    if not name:
        name = "未收錄專輯"
    d = downloads_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


LOG_LOCK = threading.Lock()

SOURCE_NAMES = {
    "www.youtube.com": "YouTube",
    "youtube.com": "YouTube",
    "m.youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "music.youtube.com": "YouTube Music",
    "soundcloud.com": "SoundCloud",
    "www.bilibili.com": "Bilibili（哔哩哔哩）",
    "bilibili.com": "Bilibili（哔哩哔哩）",
    "b23.tv": "Bilibili（哔哩哔哩）",
    "www.nicovideo.jp": "Niconico",
    "vimeo.com": "Vimeo",
    "bandcamp.com": "Bandcamp",
    "open.spotify.com": "Spotify",
}


def record_download(job, info, filepath, query):
    """把來源與連結記錄到歌曲所在專輯資料夾中的 下載記錄.txt"""
    try:
        url = info.get("webpage_url") or ""
        domain = urllib.parse.urlparse(url).netloc.lower() if url else ""
        source = SOURCE_NAMES.get(domain)
        if not source:
            for d, n in SOURCE_NAMES.items():
                if domain.endswith("." + d) or domain == d:
                    source = n
                    break
        source = source or (domain or "未知")
        lines = [
            "[%s]" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "歌曲: %s" % (("%s - %s" % (job["artist"], job["title"])) if job["artist"] else job["title"]),
            "專輯: %s" % (job.get("album") or "未收錄專輯"),
            "檔案: %s" % filepath,
            "來源: %s" % source,
            "影片標題: %s" % info.get("title", ""),
            "頻道: %s" % (info.get("uploader") or info.get("channel") or ""),
            "連結: %s" % url,
            "搜尋字: %s" % query,
            "=" * 46,
        ]
        log_file = Path(filepath).parent / "下載記錄.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with LOG_LOCK:
            is_new = not log_file.exists()
            with open(log_file, "a", encoding="utf-8") as fh:
                if is_new:
                    fh.write("\ufeff")
                fh.write("\n".join(lines) + "\n")
    except Exception:
        pass


def download_one(job, YoutubeDL):
    out_dir = album_dir(job.get("album"))
    TMP_DIR.mkdir(exist_ok=True)

    base = "%s - %s" % (job["artist"], job["title"]) if job["artist"] else job["title"]
    final_path = out_dir / (sanitize(base) + ".mp3")
    if final_path.exists():
        job.update(status="done", progress=100, message="已存在，略過", file=str(final_path))
        return

    job["status"] = "downloading"
    job["message"] = "YouTube 搜尋中…"
    # 候選搜尋字：先用正式歌名，再逐個試其他語言版本（同名同時間的變體），
    # 最後試 Bilibili。任何一個搵到就用。
    names = [job["title"]] + [a for a in (job.get("alt") or [])
                              if a and a != job["title"]]
    queries = ["ytsearch1:%s %s" % (n, job["artist"]) for n in names]
    queries += ["bilisearch1:%s %s" % (n, job["artist"]) for n in names]
    queries = [q.strip() for q in queries]

    def hook(d):
        if STOP_FLAG.is_set():
            raise StopDownload()
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["progress"] = min(95, int(d.get("downloaded_bytes", 0) * 100 / total))
            job["message"] = "下載中…"
        elif st == "finished":
            job["progress"] = 96
            job["message"] = "轉成 MP3 中…"

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(TMP_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "retries": 3,
        "socket_timeout": 30,
        "windowsfilenames": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(CONFIG.get("quality", "320")),
        }],
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG

    thumb_url = None
    info = None
    used_query = ""
    try:
        for qi, query in enumerate(queries):
            if STOP_FLAG.is_set():
                raise StopDownload()
            src = "Bilibili" if query.startswith("bilisearch") else "YouTube"
            job["message"] = "%s 搜尋中…（%d/%d）" % (src, qi + 1, len(queries))
            try:
                with YoutubeDL(opts) as ydl:
                    cand = ydl.extract_info(query, download=True)
            except StopDownload:
                raise
            except Exception as e:
                em = str(e)
                # 驗證／限速係整體問題，逐個試都冇用，直接報錯
                if "Sign in to confirm" in em or "bot" in em.lower():
                    raise
                continue
            if cand and cand.get("_type") == "playlist":
                entries = [e for e in (cand.get("entries") or []) if e]
                cand = entries[0] if entries else None
            if not cand:
                continue
            info = cand
            used_query = query
            break
        if not info:
            job.update(status="error",
                       message="YouTube／Bilibili 都找不到這首歌（已試 %d 個歌名）" % len(names))
            clean_tmp()
            return
        thumb_url = info.get("thumbnail")
        vid = info.get("id", "")
        tmp_file = None
        for p in TMP_DIR.iterdir():
            if p.stem == vid and p.suffix.lower() == ".mp3":
                tmp_file = p
                break
        if not tmp_file:
            job.update(status="error", message="轉檔失敗，找不到輸出檔")
            clean_tmp()
            return
        shutil.move(str(tmp_file), str(final_path))
    except StopDownload:
        job.update(status="stopped", message="已停止")
        clean_tmp()
        return
    except Exception as e:
        msg = str(e)
        if "Sign in to confirm" in msg or "bot" in msg.lower():
            msg = "YouTube 要求驗證（可能被限速），請稍後再試"
        job.update(status="error", message=msg[:200])
        clean_tmp()
        return

    job["progress"] = 99
    job["message"] = "寫入歌名標籤…"
    tag_mp3(final_path, job["title"], job["artist"], job["album"], thumb_url)
    record_download(job, info, str(final_path), used_query)
    job.update(status="done", progress=100, message="完成", file=str(final_path))


def clean_tmp():
    try:
        for p in TMP_DIR.iterdir():
            if p.is_file() and time.time() - p.stat().st_mtime > 300:
                p.unlink()
    except Exception:
        pass


def tag_mp3(path, title, artist, album, thumb_url=None):
    try:
        import mutagen
        from mutagen.id3 import APIC
        audio = mutagen.File(str(path), easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
        if thumb_url:
            try:
                req = urllib.request.Request(thumb_url, headers=UA)
                with urllib.request.urlopen(req, timeout=15) as r:
                    img = r.read()
                if img:
                    audio2 = mutagen.File(str(path))
                    mime = "image/jpeg"
                    if thumb_url.lower().split("?")[0].endswith(".png"):
                        mime = "image/png"
                    audio2.tags.setall("APIC", [APIC(encoding=3, mime=mime, type=3, desc="cover", data=img)])
                    audio2.save()
            except Exception:
                pass
    except Exception:
        pass


# ---------------- HTTP 伺服器 ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                html = (BASE_DIR / "index.html").read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
            elif u.path == "/api/artists":
                self._json({"artists": search_artists(q.get("q", [""])[0].strip())})
            elif u.path == "/api/songs":
                aid = q.get("artist_id", [""])[0].strip()
                country = q.get("country", [CONFIG.get("country", "TW")])[0]
                self._json(artist_albums_songs(aid, country))
            elif u.path == "/api/status":
                with JOBS_LOCK:
                    jobs = [dict(j) for j in JOBS]
                self._json({"jobs": jobs, "busy": WORKER["running"], "ffmpeg": bool(FFMPEG)})
            elif u.path == "/api/config":
                self._json({"output_dir": CONFIG.get("output_dir", ""),
                            "downloads_dir": str(downloads_dir()),
                            "country": CONFIG.get("country", "TW"),
                            "quality": CONFIG.get("quality", "320")})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        body = self._body_json()
        try:
            if u.path == "/api/download":
                tracks = body.get("tracks") or []
                if tracks:
                    add_jobs(tracks)
                self._json({"ok": True, "queued": len(tracks)})
            elif u.path == "/api/stop":
                STOP_FLAG.set()
                self._json({"ok": True})
            elif u.path == "/api/clear":
                with JOBS_LOCK:
                    JOBS[:] = [j for j in JOBS if j["status"] in ("pending", "downloading")]
                self._json({"ok": True})
            elif u.path == "/api/config":
                for k in ("output_dir", "country", "quality"):
                    if k in body and str(body[k]).strip():
                        CONFIG[k] = str(body[k]).strip()
                if "output_dir" in body:
                    try:
                        Path(CONFIG["output_dir"]).mkdir(parents=True, exist_ok=True)
                    except Exception:
                        pass
                save_config()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def main():
    check_deps()
    port = PORT_START
    server = None
    for _ in range(10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if server is None:
        log("找不到可用端口")
        sys.exit(1)
    url = "http://127.0.0.1:%d" % port
    try:
        (BASE_DIR / "app.pid").write_text(str(os.getpid()))
    except Exception:
        pass
    log("=" * 46)
    log("  MP3 音樂下載器已啟動")
    log("  請在瀏覽器開啟：%s" % url)
    log("  下載儲存位置：%s" % CONFIG.get("output_dir"))
    log("  關閉本視窗（或執行 停止下載器.bat）即結束伺服器")
    log("=" * 46)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("\n再見！")
    finally:
        try:
            (BASE_DIR / "app.pid").unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
