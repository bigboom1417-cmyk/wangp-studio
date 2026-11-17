import json, tempfile, threading, time, unittest
from pathlib import Path

import jobs


def wait(pred, timeout=5):
    t0 = time.time()
    while not pred():
        if time.time() - t0 > timeout:
            raise AssertionError("timed out")
        time.sleep(0.02)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.lib = self.d / "library"
        self.mem_calls, self.ran = [], []
        self.q = None

    def tearDown(self):
        if self.q:
            self.q.stop()
        self.tmp.cleanup()

    def mem(self, action):
        self.mem_calls.append(action)

    def video_ok(self, job, on_progress):
        self.ran.append(job["id"])
        on_progress("denoise 1/6")
        out = self.lib / f"{job['id']}.mp4"
        out.write_bytes(b"v")
        return out

    def image_ok(self, prompt, out, refs):
        self.ran.append(prompt)
        Path(out).write_bytes(b"i")
        return out

    def queue(self, run_video=None, run_image=None):
        self.q = jobs.Queue(self.d / "jobs.json", run_video or self.video_ok, run_image or self.image_ok, self.mem, self.lib)
        return self.q

    def status(self, jid):
        return next(j for j in self.q.list() if j["id"] == jid)

    def done(self, *ids):
        return lambda: all(self.status(i)["status"] in ("done", "error") for i in ids)


class QueueTests(Base):
    def test_two_videos_in_order(self):
        q = self.queue()
        a = q.add({"mode": "create", "prompt": "a", "params": {}, "files": {}})
        b = q.add({"mode": "create", "prompt": "b", "params": {}, "files": {}})
        q.start()
        wait(lambda: len(self.mem_calls) == 3)
        self.assertEqual(self.ran, [a["id"], b["id"]])
        self.assertEqual(self.mem_calls, ["free", "free", "restore"])
        self.assertEqual(self.status(a["id"])["status"], "done")
        self.assertEqual(self.status(b["id"])["file"], f"{b['id']}.mp4")
        self.assertEqual([j["id"] for j in q.list()], [b["id"], a["id"]])
        q.stop()
        self.assertEqual(self.mem_calls, ["free", "free", "restore"])
        self.assertEqual(json.loads((self.d / "jobs.json").read_text())[0]["status"], "done")

    def test_image_job_no_mem(self):
        q = self.queue()
        j = q.add({"mode": "image", "prompt": "cat", "params": {}, "files": {"refs": []}})
        q.start()
        wait(self.done(j["id"]))
        q.stop()
        self.assertEqual(self.mem_calls, [])
        self.assertEqual(self.status(j["id"])["file"], f"{j['id']}.png")

    def test_ram_error_then_next_runs(self):
        def boom(job, cb):
            if job["prompt"] == "a":
                raise OSError("[WinError 1455] The paging file is too small")
            return self.video_ok(job, cb)
        q = self.queue(run_video=boom)
        a = q.add({"mode": "create", "prompt": "a", "params": {}, "files": {}})
        b = q.add({"mode": "create", "prompt": "b", "params": {}, "files": {}})
        q.start()
        wait(lambda: "restore" in self.mem_calls)
        self.assertEqual(self.status(a["id"])["status"], "error")
        self.assertTrue(self.status(a["id"])["error"].startswith("Not enough free RAM"))
        self.assertEqual(self.status(b["id"])["status"], "done")

    def test_gemini_error_text(self):
        def bad(prompt, out, refs):
            raise RuntimeError("Gemini returned no image")
        q = self.queue(run_image=bad)
        j = q.add({"mode": "image", "prompt": "x", "params": {}, "files": {}})
        q.start()
        wait(self.done(j["id"]))
        self.assertEqual(self.status(j["id"])["error"], "Gemini returned no image")

    def test_stop_restores(self):
        go = threading.Event()
        def slow(job, cb):
            go.wait(5)
            return self.video_ok(job, cb)
        q = self.queue(run_video=slow)
        j = q.add({"mode": "create", "prompt": "a", "params": {}, "files": {}})
        q.start()
        wait(lambda: self.status(j["id"])["status"] == "running")
        self.assertFalse(q.remove(j["id"]))
        t = threading.Thread(target=q.stop)
        t.start()
        go.set()
        t.join(5)
        self.assertEqual(self.mem_calls, ["free", "restore"])

    def test_reload(self):
        (self.d / "jobs.json").write_text(json.dumps([
            {"id": "bbbbbbbb", "mode": "create", "prompt": "b", "params": {}, "files": {}, "status": "queued",
             "progress": "", "error": "", "file": "", "created": "", "took_s": 0},
            {"id": "aaaaaaaa", "mode": "create", "prompt": "a", "params": {}, "files": {}, "status": "running",
             "progress": "", "error": "", "file": "", "created": "", "took_s": 0}]))
        q = self.queue()
        self.assertEqual(self.status("aaaaaaaa")["status"], "error")
        self.assertEqual(self.status("aaaaaaaa")["error"], "server stopped")
        q.start()
        wait(self.done("bbbbbbbb"))
        self.assertEqual(self.status("bbbbbbbb")["status"], "done")

    def test_remove_deletes_file(self):
        q = self.queue()
        j = q.add({"mode": "create", "prompt": "a", "params": {}, "files": {}})
        q.start()
        wait(self.done(j["id"]))
        self.assertTrue((self.lib / f"{j['id']}.mp4").exists())
        self.assertTrue(q.remove(j["id"]))
        self.assertFalse((self.lib / f"{j['id']}.mp4").exists())
        self.assertEqual(q.list(), [])


FAKE_RUNNER = """import json, shutil, sys
from pathlib import Path
run = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out = Path(run["output_dir"]); out.mkdir()
for i, s in enumerate(run["tasks"]):
    shutil.copy(s["video_guide"], out / f"gen{i}.mp4")
    print("loading ...")
    print("PROGRESS denoise 1/6", flush=True)
    if run["model"].endswith("ref2va_hyperflow"):  # refs must reach WanGP; long clips list a partial before the final file
        ok = s.get("image_refs") and s["video_prompt_type"] == "IV-U" and s["audio_prompt_type"] == "A" and s.get("audio_guide")
        print("RESULT", json.dumps({"ok": bool(ok), "files": [str(out / "partial.mp4"), str(out / f"gen{i}.mp4")], "errors": ["refs lost"]}))
    elif run["model"].startswith("viggle_remake"):  # pins must reach WanGP, one painted frame each
        ok = s["video_prompt_type"] == "FIVU" and len(s["frames_positions"].split()) == len(s["image_refs"]) and "subject_definitions" in s["prompt"]
        print("RESULT", json.dumps({"ok": bool(ok), "files": [str(out / f"gen{i}.mp4")], "errors": ["pins lost"]}))
    else:
        print("RESULT", json.dumps({"ok": True, "files": [str(out / f"gen{i}.mp4")], "errors": []}))
"""


class RealRunVideo(Base):
    """Real run_video (ffmpeg pad/trim, frame edit, subprocess parse) with a fake runner.py, no WanGP."""

    def test_restyle_and_look(self):
        import subprocess, modes
        app = self.d / "app"
        app.mkdir()
        (app / "runner.py").write_text(FAKE_RUNNER)
        self.lib.mkdir()
        src = self.d / "src.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=24",
                        "-f", "lavfi", "-i", "sine=duration=2", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
        old = jobs.APP
        jobs.APP = app
        try:
            for mode in ("restyle", "look"):
                job = {"id": f"0000000{len(self.ran)}", "mode": mode, "prompt": "a knight",
                       "params": {"ratio": "9:16", "quality": 480, "strength": 0.5, "smooth": False, "seed": None},
                       "files": {"source": str(src)}}
                seen = []
                out = jobs.run_video(job, seen.append, self.lib, self.image_ok)
                p = modes.probe(out)
                self.assertAlmostEqual(p["seconds"], 2.0, delta=1 / 24)
                self.assertTrue(p["has_audio"])
                self.assertIn("denoise 1/6", seen)
            job = {"id": "0000000r", "mode": "refs", "prompt": "<Picture 1>",
                   "params": {"ratio": "9:16", "quality": 480, "seconds": 5, "smooth": False, "seed": None},
                   "files": {"refs": ["a.png"], "vrefs": [str(src)], "arefs": ["a.wav"]}}
            out = jobs.run_video(job, print, self.lib, self.image_ok)
            self.assertAlmostEqual(modes.probe(out)["seconds"], 2.0, delta=0.1)
        finally:
            jobs.APP = old
        self.assertEqual(len(self.ran), 2)  # look: Gemini edited the start and end frames
        self.assertIn("a knight", self.ran[0])


FAKE_PLAN = """import json, sys
# 3 s source cut at frame 40: two renders padded to the 90-frame minimum
print(json.dumps({"frames": 72, "cuts": [40], "rules": "Write six sections, up to {duration_seconds} seconds.", "groups": [
    {"render": [0, 90], "own": [0, 40], "windows": 1, "pad": 18, "pins": [20]},
    {"render": [0, 90], "own": [40, 72], "windows": 1, "pad": 18, "pins": [55]}]}))
"""


class RealGenjutsu(Base):
    """Real run_genjutsu (ffmpeg frames, clips, join, audio) with a fake plan.py, runner.py, Gemini painter and writer."""

    def test_two_shots_joined_with_source_sound(self):
        import subprocess, modes
        app = self.d / "app"
        app.mkdir()
        (app / "runner.py").write_text(FAKE_RUNNER)
        (app / "plan.py").write_text(FAKE_PLAN)
        self.lib.mkdir()
        src = self.d / "src.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=30",
                        "-f", "lavfi", "-i", "sine=duration=3", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
        painted, asked = [], []

        def paint(prompt, out, refs):
            painted.append((prompt, list(refs)))
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(refs[0]), "-vf", "scale=512:288", str(out)], check=True)
            return out

        def ask(prompt, refs=()):
            asked.append(prompt)
            return ""  # Gemini down: the fixed template is used

        job = {"id": "0000000g", "mode": "genjutsu", "prompt": "a knight in armor",
               "params": {"quality": 480, "gmode": "swap", "style": "", "pins": "", "upscale": False},
               "files": {"source": str(src), "refs": [str(src)]}}
        old = jobs.APP
        jobs.APP = app
        try:
            out = jobs.run_genjutsu(job, print, self.lib, paint, ask)
        finally:
            jobs.APP = old
        p = modes.probe(out)
        self.assertAlmostEqual(p["seconds"], 3.0, delta=0.1)
        self.assertEqual(p["size"], (832, 480))
        self.assertTrue(p["has_audio"])
        self.assertEqual(len(painted), 2)
        self.assertEqual(len(painted[1][1]), 3)  # the frame, the first painted frame, his reference
        self.assertIn("up to 3.75 seconds", asked[0])
        self.assertTrue((self.lib / "0000000g_prompts.txt").exists())


class ParserTests(unittest.TestCase):
    def test_progress(self):
        seen = []
        self.assertIsNone(jobs.parse_line("PROGRESS denoise 3/8", seen.append))
        self.assertEqual(seen, ["denoise 3/8"])
        self.assertIsNone(jobs.parse_line("loading model...", seen.append))

    def test_result(self):
        self.assertEqual(jobs.parse_line('RESULT {"ok": true, "files": ["a.mp4"], "errors": []}', print), ["a.mp4"])
        with self.assertRaisesRegex(RuntimeError, "x"):
            jobs.parse_line('RESULT {"ok": false, "files": [], "errors": ["x"]}', print)


if __name__ == "__main__":
    unittest.main()
