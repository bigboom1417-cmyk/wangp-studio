"""Studio API on port 4500: job queue (jobs.py) + library files + the page (static/index.html)."""
import json, re, secrets, shutil, subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import jobs, manga, modes

APP = Path(__file__).parent
FILES = {"image": ["refs"], "create": ["start", "end"], "restyle": ["source"], "look": ["source", "start", "end"],
         "refs": ["refs", "vrefs", "arefs"], "genjutsu": ["source", "refs"], "manga": ["refs", "pagerefs"]}
MAX = {"refs": 9, "vrefs": 3, "arefs": 3}  # MiniMax H3 limit (pipeline.py): 9 pictures, 3 videos, 3 audio
LONGEST = {"restyle": 30.5, "look": 5.8, "genjutsu": 30.5}  # source seconds; Viggle look runs one 124-frame window
GMODES = ("swap", "edit", "auto")  # Genjutsu: Motion Transfer, Object Swap, Lite


def is_image(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name:stream=width", "-of", "json",
                        str(path)], capture_output=True, text=True)
    if r.returncode:
        return False
    info = json.loads(r.stdout)
    fmt = info.get("format", {}).get("format_name", "")
    return (fmt == "image2" or fmt.endswith("_pipe")) and any(s.get("width") for s in info.get("streams", []))


def number(value, name, lo, hi):
    try:
        x = float(value)
    except ValueError:
        x = None
    if x is None or not lo <= x <= hi:
        raise HTTPException(400, f"{name} must be a number from {lo} to {hi}.")
    return x


def create_app(queue=None, library=jobs.LIB):
    library = Path(library)
    q = queue or jobs.Queue(library=library)

    @asynccontextmanager
    async def lifespan(app):
        q.start()
        yield
        q.stop()

    app = FastAPI(lifespan=lifespan)

    @app.post("/api/jobs")
    def add_job(mode: str = Form(""), prompt: str = Form(""), ratio: str = Form("9:16"), quality: str = Form("480"),
                seconds: str = Form("5"), strength: str = Form("0.5"), smooth: str = Form("0"),
                source_desc: str = Form(""), gmode: str = Form("swap"), style: str = Form(""), pins: str = Form(""),
                upscale: str = Form("0"), lang: str = Form("ar"), chapters: str = Form("1"), pages: str = Form("4"),
                source: Optional[UploadFile] = File(None), pagerefs: List[UploadFile] = File([]),
                start: Optional[UploadFile] = File(None), end: Optional[UploadFile] = File(None),
                refs: List[UploadFile] = File([]), vrefs: List[UploadFile] = File([]),
                arefs: List[UploadFile] = File([])):
        if mode not in FILES:
            raise HTTPException(400, f"Unknown mode \"{mode}\"; use image, create, refs, restyle, look, genjutsu or manga.")
        if mode == "manga" and (style not in manga.STYLES or lang not in manga.LANGS):
            raise HTTPException(400, f"Manga style must be one of {', '.join(manga.STYLES)}; language one of {', '.join(manga.LANGS)}.")
        if mode == "genjutsu" and gmode not in GMODES:
            raise HTTPException(400, "Genjutsu type must be swap, edit or auto.")
        if mode == "genjutsu" and not re.fullmatch(r"\s*(\d+(\.\d+)?\s*(,\s*\d+(\.\d+)?\s*)*)?", pins):
            raise HTTPException(400, "Pin times are seconds separated by commas, like 1.5, 6.")
        if not prompt.strip():
            raise HTTPException(400, "Write a prompt first.")
        if ratio not in ("9:16", "16:9", "1:1"):
            raise HTTPException(400, "Ratio must be 9:16, 16:9 or 1:1.")
        if quality not in ("480", "720"):
            raise HTTPException(400, "Quality must be 480 or 720.")
        params = {"ratio": ratio, "quality": int(quality), "smooth": smooth == "1", "seed": None,
                  "seconds": number(seconds, "Length", 2, 30) if mode in ("create", "refs") else None,
                  "strength": number(strength, "Strength", 0.3, 0.8) if mode == "restyle" else None,
                  "source_desc": source_desc.strip()}
        if mode == "genjutsu":
            params.update(gmode=gmode, style=style.strip(), pins=pins.replace(" ", ""), upscale=upscale == "1", smooth=False)
        if mode == "manga":
            params = {"style": style, "lang": lang, "chapters": int(number(chapters, "Chapters", 1, 5)),
                      "pages": int(number(pages, "Pages per chapter", 1, 8)), "ratio": "2:3"}
        given = {"source": [source], "start": [start], "end": [end], "refs": refs, "vrefs": vrefs, "arefs": arefs,
                 "pagerefs": pagerefs}
        jid = secrets.token_hex(4)
        uploads = library / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        saved, files = [], {}
        try:
            for field in FILES[mode]:
                ups = [u for u in given[field] if u and u.filename]
                if mode == "genjutsu" and field == "refs" and len(ups) > 8:
                    raise HTTPException(400, "At most 8 reference pictures (Gemini takes 10 files with the frame).")
                if mode == "manga" and len(ups) > (12 if field == "pagerefs" else manga.MAX_CAST_REFS):
                    raise HTTPException(400, "At most 9 cast pictures and 12 manga pages.")
                if mode == "refs" and len(ups) > MAX[field]:
                    raise HTTPException(400, "At most 9 pictures, 3 videos and 3 audio clips, 12 in total.")
                for i, up in enumerate(ups):
                    ext = Path(up.filename.replace("\\", "/")).suffix.lower()
                    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
                        raise HTTPException(400, f"The {field} file needs a normal file extension like .mp4 or .png.")
                    dst = uploads / f"{jid}_{field}{i if field.endswith('refs') else ''}{ext}"
                    with dst.open("wb") as out:
                        shutil.copyfileobj(up.file, out)
                    saved.append(dst)
                    if field == "source":
                        try:
                            secs = 0 if is_image(dst) else modes.probe(dst)["seconds"]
                        except Exception:
                            secs = 0
                        if secs <= 0:
                            raise HTTPException(400, "The source is not a video file that can be read.")
                        if secs > LONGEST[mode]:
                            raise HTTPException(400, f"Source is longer than {LONGEST[mode]:g} s; cut it into shots first")
                    elif field in ("vrefs", "arefs"):
                        kind = "video" if field == "vrefs" else "audio"
                        try:
                            secs = modes.seconds(dst) if field == "arefs" or not is_image(dst) else 0
                        except Exception:
                            secs = 0
                        if secs < 2:
                            raise HTTPException(400, f"Each reference {kind} must be a {kind} file of at least 2 s.")
                    elif not is_image(dst):
                        raise HTTPException(400, f"The {field} file is not an image that can be read.")
                    if field.endswith("refs"):
                        files.setdefault(field, []).append(str(dst))
                    else:
                        files[field] = str(dst)
            if mode in ("restyle", "look", "genjutsu") and "source" not in files:
                raise HTTPException(400, "This mode needs a source video.")
            if mode == "refs":
                visual = len(files.get("refs", [])) + len(files.get("vrefs", []))
                if not visual:
                    raise HTTPException(400, "Add at least one picture or video reference.")
                if len(files.get("arefs", [])) > visual:
                    raise HTTPException(400, "Add at least as many pictures/videos as audio clips (model rule).")
                if visual + len(files.get("arefs", [])) > 12:
                    raise HTTPException(400, "At most 12 references in total (9 pictures, 3 videos, 3 audio each at most).")
            if mode == "genjutsu":
                params["ratio"] = modes.ratio_of(*modes.probe(files["source"])["size"])
        except Exception:
            for p in saved:
                p.unlink(missing_ok=True)
            raise
        return q.add({"id": jid, "mode": mode, "prompt": prompt.strip().replace("\r\n", "\n"), "params": params, "files": files})

    @app.get("/api/jobs")
    def list_jobs():
        js = q.list()
        return {"jobs": js, "busy": any(j["status"] in ("queued", "running") for j in js)}

    @app.delete("/api/jobs/{jid}")
    def delete_job(jid: str):
        if not any(j["id"] == jid for j in q.list()):
            raise HTTPException(404, "No such job.")
        if not q.remove(jid):
            raise HTTPException(409, "That job is running; wait until it ends.")
        return {"ok": True}

    @app.get("/library/{name}")
    def library_file(name: str):
        p = (library / name).resolve()
        if p.parent != library.resolve() or not p.is_file():
            raise HTTPException(404, "Not found.")
        return FileResponse(p)

    def book_dir(bid):
        d = (library / "manga" / bid).resolve()
        if d.parent != (library / "manga").resolve() or not (d / "book.json").is_file():
            raise HTTPException(404, "No such book.")
        return d

    @app.get("/api/manga/{bid}")
    def get_book(bid: str):
        return manga.load(book_dir(bid))

    @app.get("/manga/{bid}/{name}")
    def book_file(bid: str, name: str):
        d = book_dir(bid)
        p = (d / name).resolve()
        if p.parent != d or not p.is_file():
            raise HTTPException(404, "Not found.")
        return FileResponse(p)

    @app.put("/api/manga/{bid}/page/{n}/bubbles")
    def save_bubbles(bid: str, n: int, bubbles: List[dict] = Body(...)):
        d = book_dir(bid)
        book = manga.load(d)
        if not 0 <= n < len(book["pages"]):
            raise HTTPException(404, "No such page.")
        keys = ("who", "text", "kind", "x", "y", "w", "h")
        book["pages"][n]["bubbles"] = [{k: b.get(k) for k in keys} for b in bubbles]
        manga._save(d, book)
        return {"ok": True}

    @app.post("/api/manga/{bid}/page/{n}")
    def page_job(bid: str, n: int, action: str = Form("redraw"), prompt: str = Form("")):
        book = manga.load(book_dir(bid))
        if not 0 <= n < len(book["pages"]):
            raise HTTPException(404, "No such page.")
        if action not in ("redraw", "edit") or (action == "edit" and not prompt.strip()):
            raise HTTPException(400, "Say what to change on the page.")
        if action == "redraw" and not book["pages"][n].get("panels"):
            raise HTTPException(400, "Recreated pages can only be edited with a prompt.")
        text = prompt.strip() or f"Redraw page {n + 1} of {book.get('title', 'the book')}"
        return q.add({"mode": "manga_page", "prompt": text, "params": {"book": bid, "page": n, "action": action, "ratio": "2:3"}})

    @app.post("/api/manga/{bid}/export")
    def export(bid: str, fmt: str = Form(...), pages: List[UploadFile] = File(...)):
        import io, zipfile
        from PIL import Image
        d = book_dir(bid)
        if fmt not in ("pdf", "cbz", "webtoon"):
            raise HTTPException(400, "Export as pdf, cbz or webtoon.")
        data = [p.file.read() for p in pages]  # pages lettered by the browser, in order
        out = d / f"export.{'png' if fmt == 'webtoon' else fmt}"
        if fmt == "cbz":
            with zipfile.ZipFile(out, "w") as z:
                for i, b in enumerate(data):
                    z.writestr(f"{i + 1:03d}.png", b)
        else:
            imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in data]
            if fmt == "pdf":
                imgs[0].save(out, save_all=True, append_images=imgs[1:])
            else:  # webtoon: one long strip at the first page's width
                w = imgs[0].width
                imgs = [im.resize((w, round(im.height * w / im.width))) for im in imgs]
                strip = Image.new("RGB", (w, sum(im.height for im in imgs)), "white")
                y = 0
                for im in imgs:
                    strip.paste(im, (0, y))
                    y += im.height
                strip.save(out)
        return FileResponse(out, filename=f"{manga.load(d).get('title') or bid}.{out.suffix[1:]}")

    @app.get("/")
    def page():
        return FileResponse(APP / "static" / "index.html")

    return app


app = create_app()
