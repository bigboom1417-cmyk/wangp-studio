import re, subprocess, tempfile, threading, unittest
from pathlib import Path

from fastapi.testclient import TestClient

import jobs, server


def clip(path, seconds):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=24",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.short, cls.long, cls.png = d / "short.mp4", d / "long.mp4", d / "a.png"
        clip(cls.short, 2)
        clip(cls.long, 7)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64", "-frames:v", "1", str(cls.png)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        d = Path(self.t.name)
        self.lib = d / "library"
        self.go = threading.Event()

        def slow(job, cb):  # keeps the first job running so later ones stay queued
            self.go.wait(10)
            return self.lib / "x.mp4"
        q = jobs.Queue(d / "jobs.json", slow, lambda p, o, r: o, lambda a: None, self.lib)
        self.c = TestClient(server.create_app(q, self.lib))
        self.c.__enter__()

    def tearDown(self):
        self.go.set()
        self.c.__exit__(None, None, None)
        self.t.cleanup()

    def post(self, files=None, **data):
        return self.c.post("/api/jobs", data={"prompt": "a cat", **data}, files=files or {})

    def test_create_listed(self):
        r = self.post(mode="create")
        self.assertEqual(r.status_code, 200, r.text)
        job = r.json()
        self.assertRegex(job["id"], "^[0-9a-f]{8}$")
        self.assertEqual(job["params"]["ratio"], "9:16")
        listed = self.c.get("/api/jobs").json()
        self.assertEqual(listed["jobs"][0]["id"], job["id"])
        self.assertTrue(listed["busy"])

    def test_restyle_needs_source(self):
        r = self.post(mode="restyle")
        self.assertEqual(r.status_code, 400)
        self.assertIn("source", r.json()["detail"])

    def test_unreadable_source(self):
        r = self.post(mode="restyle", files={"source": ("x.mp4", b"not a video", "video/mp4")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(list((self.lib / "uploads").glob("*")), [])

    def test_long_source(self):
        r = self.post(mode="look", files={"source": ("long.mp4", self.long.read_bytes(), "video/mp4")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("5.8 s", r.json()["detail"])
        self.assertEqual(self.c.get("/api/jobs").json()["jobs"], [])
        r = self.post(mode="restyle", files={"source": ("long.mp4", self.long.read_bytes(), "video/mp4")})
        self.assertEqual(r.status_code, 200, r.text)

    def test_create_30s(self):
        self.assertEqual(self.post(mode="create", seconds="30").status_code, 200)
        self.assertEqual(self.post(mode="create", seconds="31").status_code, 400)

    def test_refs(self):
        png, vid = ("a.png", self.png.read_bytes(), "image/png"), ("s.mp4", self.short.read_bytes(), "video/mp4")
        r = self.post(mode="refs", seconds="10", files=[("refs", png), ("refs", png), ("vrefs", vid), ("arefs", vid)])
        self.assertEqual(r.status_code, 200, r.text)
        f = r.json()["files"]
        self.assertEqual((len(f["refs"]), len(f["vrefs"]), len(f["arefs"])), (2, 1, 1))
        self.assertIn("at least one picture", self.post(mode="refs").json()["detail"])
        r = self.post(mode="refs", files=[("refs", png)] * 10)
        self.assertIn("9 pictures", r.json()["detail"])
        r = self.post(mode="refs", files=[("refs", png), ("arefs", vid), ("arefs", vid)])
        self.assertIn("as many pictures/videos as audio", r.json()["detail"])
        r = self.post(mode="refs", files=[("refs", png)] * 9 + [("vrefs", vid)] * 2 + [("arefs", vid)] * 2)
        self.assertIn("12 references in total", r.json()["detail"])
        r = self.post(mode="refs", files=[("vrefs", png)])
        self.assertIn("at least 2 s", r.json()["detail"])

    def test_image_as_start_and_bad_ref(self):
        r = self.post(mode="create", files={"start": ("s.png", self.png.read_bytes(), "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertRegex(Path(r.json()["files"]["start"]).name, r"^[0-9a-f]{8}_start\.png$")
        r = self.post(mode="image", files={"refs": ("r.png", b"junk", "image/png")})
        self.assertEqual(r.status_code, 400)

    def test_upload_name_sanitised(self):
        r = self.post(mode="look", files={"source": ("..\\..\\x مرحبا.mp4", self.short.read_bytes(), "video/mp4")})
        self.assertEqual(r.status_code, 200, r.text)
        src = Path(r.json()["files"]["source"])
        self.assertRegex(src.name, r"^[0-9a-f]{8}_source\.mp4$")
        self.assertEqual(src.parent, self.lib / "uploads")

    def test_library_cannot_escape(self):
        self.lib.mkdir(parents=True, exist_ok=True)
        (self.lib / "ok.png").write_bytes(b"x")
        self.assertEqual(self.c.get("/library/ok.png").status_code, 200)
        self.assertEqual(self.c.get("/library/..%5Cjobs.json").status_code, 404)
        self.assertEqual(self.c.get("/library/uploads").status_code, 404)

    def test_unknown_mode(self):
        self.assertEqual(self.post(mode="dance").status_code, 400)

    def test_delete(self):
        running = self.post(mode="create").json()["id"]
        queued = self.post(mode="create").json()["id"]
        from test_jobs import wait
        wait(lambda: self.c.get("/api/jobs").json()["jobs"][-1]["status"] == "running")
        self.assertEqual(self.c.delete(f"/api/jobs/{queued}").status_code, 200)
        self.assertNotIn(queued, [j["id"] for j in self.c.get("/api/jobs").json()["jobs"]])
        self.assertEqual(self.c.delete(f"/api/jobs/{running}").status_code, 409)
        self.assertEqual(self.c.delete("/api/jobs/ffffffff").status_code, 404)


if __name__ == "__main__":
    unittest.main()
