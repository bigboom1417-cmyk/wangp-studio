"""Job list + one worker thread. Video jobs run WanGP in a runner.py subprocess so all RAM/VRAM is returned after each."""
import json, os, secrets, shutil, subprocess, sys, tempfile, threading, time
from collections import deque
from datetime import datetime
from functools import partial
from pathlib import Path

import psutil

import gemini_api, modes

APP = Path(__file__).parent
LIB = Path(os.environ.get("STUDIO_LIBRARY", APP.parent / "library"))
WANGP = os.environ.get("WANGP_DIR", r"C:\AI\WanGP")
MEM = APP.parent / "mem.ps1"  # optional: frees RAM before a job (stops your heavy apps) and restores them after
# Gemini: a browser helper script (node, same CLI as gemini-web-image/ask.cjs) if set, else the official API (GEMINI_API_KEY).
GEMINI = os.environ.get("STUDIO_GEMINI_IMAGE")
ASK = os.environ.get("STUDIO_GEMINI_ASK")


def mem(action):
    if MEM.exists():
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(MEM), action])


def run_image(prompt, out, refs):
    if not GEMINI:
        return gemini_api.image(prompt, out, refs)
    cmd = ["node", GEMINI, prompt, "--out", str(out)]
    for ref in list(refs)[:10]:  # Gemini takes 10 files per message
        cmd += ["--ref", str(ref)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError((r.stderr.strip().splitlines() or [f"Gemini tool exit {r.returncode}"])[-1])
    return out


def ask(prompt, refs=()):
    """Gemini's text reply, or "" when Gemini fails (callers fall back)."""
    if not ASK:
        try:
            return gemini_api.ask(prompt, refs)
        except Exception:
            return ""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(prompt)
    cmd = ["node", ASK, "--file", f.name]
    for ref in list(refs)[:10]:
        cmd += ["--ref", str(ref)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=420)
    except subprocess.TimeoutExpired:
        return ""
    finally:
        os.unlink(f.name)
    out = r.stdout.strip()
    return "" if r.returncode or "went wrong" in out or "encountered an error" in out else out


def parse_line(line, on_progress):
    """PROGRESS -> on_progress(text); RESULT -> files or raise; anything else -> None."""
    if line.startswith("PROGRESS "):
        on_progress(line[9:])
    elif line.startswith("RESULT "):
        res = json.loads(line[7:])
        if not res["ok"] or not res["files"]:
            raise RuntimeError("; ".join(res["errors"]) or "WanGP made no file")
        return res["files"]
    return None


def ffmpeg(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError("ffmpeg: " + (r.stderr.strip().splitlines() or ["failed"])[-1])


def wangp(model, tasks, tmp, on_progress):
    """Runs the tasks in one runner.py process (one model load); returns each task's final file."""
    low = psutil.virtual_memory().available < 7 * 2**30
    run = {"model": model, "tasks": tasks, "output_dir": str(tmp / "out"),
           "cli_args": ["--profile", "5", "--perc-reserved-mem-max", "0.1"] if low else []}
    (tmp / "run.json").write_text(json.dumps(run), encoding="utf-8")
    on_progress("loading WanGP")
    tail, finals = deque(maxlen=20), []
    with subprocess.Popen([sys.executable, str(APP / "runner.py"), str(tmp / "run.json")], cwd=WANGP,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"}) as proc:
        for line in proc.stdout:
            line = line.rstrip()
            got = parse_line(line, on_progress)
            if got:
                finals.append(got[-1])  # long clips also save each earlier window's partial video first
            elif not line.startswith(("PROGRESS ", "RESULT ")):
                tail.append(line)
    if len(finals) < len(tasks):
        raise RuntimeError("\n".join(tail) or f"runner exit {proc.returncode}")
    return finals


def run_video(job, on_progress, library=LIB, run_image=run_image):
    if job["mode"] == "genjutsu":
        return run_genjutsu(job, on_progress, library, run_image)
    p, f, mode = job["params"], job["files"], job["mode"]
    tmp = Path(tempfile.mkdtemp(prefix="studio_"))
    try:
        spec = {**p, "mode": mode, "prompt": job["prompt"], "start": f.get("start"), "end": f.get("end"), "source": None,
                "refs": f.get("refs"), "vrefs": f.get("vrefs"), "arefs": f.get("arefs")}
        if f.get("source"):
            src_s = modes.probe(f["source"])["seconds"]
            spec["frames"] = modes.frames_for(src_s)
            spec["source"] = str(tmp / "pad.mp4")
            on_progress("preparing source")
            ffmpeg(modes.pad_cmd(f["source"], spec["source"], spec["frames"]))
        else:
            spec["frames"] = modes.frames_for(float(p["seconds"]))
        if mode == "look":
            last = spec["frames"] - 1
            if not (spec["start"] and spec["end"]):
                who = p.get("source_desc") or "the person"
                edit = f"Edit this picture: turn {who} into {job['prompt']}. Keep the pose, framing and camera exactly the same."
                for key, n in (("start", 0), ("end", last)):
                    if not spec[key]:
                        frame = tmp / f"{key}_frame.png"
                        ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", spec["source"], "-vf", f"select=eq(n\\,{n})",
                                "-frames:v", "1", str(frame)])
                        on_progress(f"Gemini edits the {key} frame")
                        spec[key] = str(run_image(edit, tmp / f"{key}.png", [frame]))
            spec["prompt"] = modes.look_prompt(job["prompt"], p.get("source_desc", ""), last / 24)
        model, overrides = modes.settings(spec)
        out = Path(library) / f"{job['id']}.mp4"
        final = wangp(model, [overrides], tmp, on_progress)[0]
        if f.get("source"):
            on_progress("trimming to source length")
            ffmpeg(modes.trim_cmd(final, f["source"], out, src_s))
        else:
            shutil.move(final, out)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def still(src, out, size, n=None):
    """Frame n of a video (or a picture when n is None), filled and cropped to size."""
    pick = [f"select=eq(n\\,{n})"] if n is not None else []
    vf = ",".join(pick + [f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase,crop={size[0]}:{size[1]}"])
    ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", vf, "-frames:v", "1", str(out)])
    return out


def run_genjutsu(job, on_progress, library=LIB, run_image=run_image, ask=ask):
    """Paint one frame per shot with Gemini, then Viggle Remake carries the motion and keeps the sound (design 17 Nov)."""
    import pins
    p, f, desc, style = job["params"], job["files"], job["prompt"], job["params"].get("style", "")
    refs = f.get("refs", [])[:8]  # + the frame + the first painted frame = Gemini's 10 files
    tmp = Path(tempfile.mkdtemp(prefix="genjutsu_"))
    try:
        info = modes.probe(f["source"])
        size = modes.VSIZE[(modes.ratio_of(*info["size"]), int(p["quality"]))]
        src = tmp / "src.mp4"
        on_progress("preparing source")
        ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", f["source"], "-vf", "fps=24", *modes.ENC, str(src)])
        mode = p["gmode"]
        if mode == "auto":  # Genjutsu Lite: the change type is picked for him
            on_progress("Gemini picks swap or edit")
            mode = "edit" if ask(modes.lite_question(desc)).strip().lower().startswith("edit") else "swap"
        on_progress("finding shots")
        r = subprocess.run([sys.executable, str(APP / "plan.py"), str(src), mode, p.get("pins", "")], cwd=WANGP,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("{")), None)
        if not line:
            raise RuntimeError("plan: " + ((r.stderr or r.stdout).strip().splitlines() or ["failed"])[-1])
        plan = json.loads(line)
        groups, painted, first = plan["groups"], {}, None
        todo = sorted({n for g in groups for n in g["pins"]})
        for i, n in enumerate(todo, 1):
            on_progress(f"Gemini paints frame {i}/{len(todo)}")
            raw = still(src, tmp / f"f{n}.png", size, n)
            got = run_image(modes.paint_instruction(mode, desc, style, len(refs), first is not None, n / 24), tmp / f"g{n}.png",
                            [raw] + ([first] if first else []) + refs)
            painted[n] = still(got, tmp / f"p{n}.png", size)
            first = first or painted[n]
        tasks = []
        for gi, g in enumerate(groups):
            r0, r1 = g["render"]
            clip = tmp / f"clip{gi}.mp4"
            ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
                    f"trim=start_frame={r0}:end_frame={r1},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop={g['pad']}",
                    "-af", f"atrim={r0 / 24}:{r1 / 24},asetpts=PTS-STARTPTS,apad=whole_dur={(r1 - r0) / 24}",
                    *modes.ENC, str(clip)])
            rel, prompts = [n - r0 for n in g["pins"]], []
            for k in range(g["windows"]):
                lines, idx = modes.window_pictures(rel, k, g["windows"])
                ws, we = k * pins.HOP, min(k * pins.HOP + pins.WIN, r1 - r0)
                cuts_s = [(c - r0 - ws) / 24 for c in plan["cuts"] if r0 + ws < c < r0 + we]
                on_progress(f"Gemini writes prompt {len(tasks) + 1}.{k + 1}")
                req = modes.prompt_request(plan["rules"], desc, style, (we - ws) / 24, lines, cuts_s, p.get("source_desc", ""))
                text = modes.clean_prompt(ask(req, [painted[g["pins"][i]] for i in idx]))
                prompts.append(text or modes.fallback_prompt(mode, desc, lines, (we - ws) / 24))
            model, s = modes.genjutsu_settings(mode, size, str(clip), g, [str(painted[n]) for n in g["pins"]], prompts,
                                               p.get("upscale"))
            tasks.append(s)
        (Path(library) / f"{job['id']}_prompts.txt").write_text("\n\n----\n\n".join(t["prompt"] for t in tasks), encoding="utf-8")
        takes = wangp(model, tasks, tmp, on_progress)
        on_progress("joining shots")
        k = 2 if p.get("upscale") else 1
        out = Path(library) / f"{job['id']}.mp4"
        cmd = ["ffmpeg", "-v", "error", "-y"] + [x for t in takes for x in ("-i", t)] + ["-i", str(src)]
        ffmpeg(cmd + ["-filter_complex", modes.take_filter(len(takes), [g["own"] for g in groups], [g["render"] for g in groups],
                                                          (size[0] * k, size[1] * k)),
                      "-map", "[v]", "-map", f"{len(takes)}:a?", "-r", "24", *modes.ENC, str(out)])
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_manga(job, on_progress, library=LIB, run_image=run_image, ask=ask):
    """A whole book (mode manga) or one page redrawn / edited (mode manga_page); the card shows a copy of that page."""
    import manga
    p = job["params"]
    if job["mode"] == "manga":
        page = manga.run(job, Path(library) / "manga" / job["id"], run_image, ask, on_progress)
    else:
        d = Path(library) / "manga" / p["book"]
        book = manga.load(d)
        manga.draw_page(book, d, p["page"], run_image, ask, on_progress, edit=job["prompt"] if p["action"] == "edit" else "")
        page = d / book["pages"][p["page"]]["image"]
    out = Path(library) / f"{job['id']}.png"
    shutil.copy(page, out)
    return out


class Queue:
    def __init__(self, path=APP / "jobs.json", run_video=None, run_image=run_image, mem=mem, library=LIB):
        self.path, self.library = Path(path), Path(library)
        self.run_image, self.mem = run_image, mem
        self.run_video = run_video or partial(globals()["run_video"], library=self.library, run_image=run_image)
        self.lock, self.wake = threading.Lock(), threading.Event()
        self.halt, self.thread = False, None
        self.jobs = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        stale = [j for j in self.jobs if j["status"] == "running"]
        for j in stale:
            j.update(status="error", error="server stopped")
        # a job died mid-run last time, so the PC may still be in "freed" state
        self.freed = bool(stale)
        if stale:
            self._save()

    def _save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.jobs, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    def add(self, job):
        job = {"id": secrets.token_hex(4), "params": {}, "files": {}, **job, "status": "queued", "progress": "",
               "error": "", "file": "", "created": datetime.now().isoformat(timespec="seconds"), "took_s": 0}
        with self.lock:
            self.jobs.append(job)
        self._save()
        self.wake.set()
        return dict(job)

    def list(self):
        with self.lock:
            return [dict(j) for j in reversed(self.jobs)]

    def remove(self, jid):
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == jid), None)
            if not job or job["status"] == "running":
                return False
            self.jobs.remove(job)
        if job["file"]:
            (self.library / job["file"]).unlink(missing_ok=True)
        (self.library / f"{jid}_prompts.txt").unlink(missing_ok=True)
        self._save()
        return True

    def start(self):
        self.halt = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        # shortcut: waits for a running job to finish; a hard kill skips restore until the next start
        self.halt = True
        self.wake.set()
        if self.thread:
            self.thread.join()
            self.thread = None
        if self.freed:
            self.mem("restore")
            self.freed = False

    def _loop(self):
        while not self.halt:
            with self.lock:
                job = next((j for j in self.jobs if j["status"] == "queued"), None)
            if job:
                self._run(job)
                continue
            if self.freed:
                self.mem("restore")
                self.freed = False
            self.wake.wait(2)
            self.wake.clear()

    def _run(self, job):
        job.update(status="running", progress="", error="")
        self._save()
        t0 = time.time()
        self.library.mkdir(parents=True, exist_ok=True)
        try:
            if job["mode"] == "image":
                out = self.run_image(job["prompt"], self.library / f"{job['id']}.png", job["files"].get("refs", []))
            elif job["mode"] in ("manga", "manga_page"):  # Gemini only: nothing to free
                out = run_manga(job, lambda text: job.update(progress=text), self.library, self.run_image)
            else:
                self.mem("free")
                self.freed = True
                out = self.run_video(job, lambda text: job.update(progress=text))
            job.update(status="done", file=Path(out).name, progress="")
        except Exception as e:
            msg = str(e) or type(e).__name__
            if "1455" in msg or "insufficient vram" in msg.lower():
                free = psutil.virtual_memory().available / 2**30
                msg = f"Not enough free RAM ({free:.1f} GB). Close apps and retry."
            job.update(status="error", error=msg)
        job["took_s"] = int(time.time() - t0)
        self._save()
