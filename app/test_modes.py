import subprocess, tempfile, unittest
from pathlib import Path

import modes


def job(**kw):
    j = {"mode": "create", "prompt": "p", "ratio": "9:16", "quality": 480, "seconds": 5, "strength": 0.5,
         "smooth": False, "seed": None, "source": None, "start": None, "end": None, "frames": 124}
    j.update(kw)
    return j


class Frames(unittest.TestCase):
    def test_frames(self):
        self.assertEqual(modes.frames_for(5), 124)
        self.assertEqual(modes.frames_for(4.4), 107)
        self.assertEqual(modes.frames_for(5.8), 141)


class Settings(unittest.TestCase):
    def test_create(self):
        m, s = modes.settings(job(start="a.png", end="b.png"))
        self.assertEqual(m, "minimax_h3_fl2va_hyperflow")
        self.assertEqual(s["config"], "distilled_4b")
        self.assertEqual(s["resolution"], "480x832")
        self.assertEqual(s["video_length"], 124)
        self.assertEqual(s["image_start"], ["a.png"])
        self.assertEqual(s["image_end"], ["b.png"])
        self.assertEqual(s["seed"], 42)
        self.assertNotIn("temporal_upsampling", s)

    def test_long_create_windows(self):
        _, s = modes.settings(job(frames=modes.frames_for(30)))
        self.assertEqual(s["sliding_window_size"], modes.WINDOW)
        self.assertEqual(s["multi_prompts_gen_type"], "FG")
        _, s = modes.settings(job(prompt="part one\n\npart two"))
        self.assertNotIn("sliding_window_size", s)
        self.assertEqual(s["multi_prompts_gen_type"], "PW")

    def test_refs(self):
        m, s = modes.settings(job(mode="refs", refs=["a.png", "b.png"], vrefs=["v1.mp4", "v2.mp4"], arefs=["s.wav"]))
        self.assertEqual(m, "minimax_h3_ref2va_hyperflow")
        self.assertEqual((s["video_prompt_type"], s["audio_prompt_type"]), ("IV+-U", "A"))
        self.assertEqual((s["image_refs"], s["video_guide"], s["video_guide2"], s["audio_guide"]), (["a.png", "b.png"], "v1.mp4", "v2.mp4", "s.wav"))
        _, s = modes.settings(job(mode="refs", vrefs=["v.mp4"], arefs=["1.wav", "2.wav", "3.wav"]))
        self.assertEqual((s["video_prompt_type"], s["audio_prompt_type"], s["audio_guide3"]), ("V-U", "ABD", "3.wav"))
        self.assertNotIn("image_refs", s)

    def test_create_text_only(self):
        _, s = modes.settings(job(ratio="16:9", quality=720))
        self.assertEqual(s["resolution"], "1280x720")
        self.assertNotIn("image_start", s)
        self.assertNotIn("image_end", s)

    def test_restyle(self):
        m, s = modes.settings(job(mode="restyle", strength=0.5, source="src.mp4", seed=7, smooth=True))
        self.assertEqual(m, "minimax_h3_fl2va_hyperflow")
        self.assertEqual(s["video_prompt_type"], "GV")
        self.assertEqual(s["denoising_strength"], 0.5)
        self.assertEqual(s["audio_prompt_type"], "KS")
        self.assertEqual(s["video_guide"], "src.mp4")
        self.assertEqual(s["video_length"], 124)
        self.assertEqual(s["seed"], 7)
        self.assertEqual(s["temporal_upsampling"], "rife*2")

    def test_look(self):
        m, s = modes.settings(job(mode="look", source="src.mp4", start="a.png", end="b.png", frames=124))
        self.assertEqual(m, "viggle_remake")
        self.assertEqual(s["multi_prompts_gen_type"], "FG")
        self.assertEqual(s["video_prompt_type"], "FIVU")
        self.assertEqual(s["frames_positions"], "1 124")
        self.assertEqual(s["image_refs"], ["a.png", "b.png"])
        self.assertEqual(s["video_guide"], "src.mp4")
        self.assertEqual(s["audio_prompt_type"], "K")
        self.assertEqual(s["video_length"], 124)
        self.assertEqual(s["sliding_window_size"], 124)

    def test_look_prompt(self):
        p = modes.look_prompt("a knight", "a man", 5.125)
        self.assertIn("a knight", p)
        self.assertIn("a man", p)
        self.assertIn("00:05.125", p)
        self.assertNotIn("Terminator", p)
        self.assertIn("the person in the source video", modes.look_prompt("a knight", "", 5))


def ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)


def count_frames(p):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                   "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(p)], text=True)
    return int(out.strip())


class Ffmpeg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.src = self.d / "src.mp4"
        ff("-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=24", "-f", "lavfi", "-i", "sine=duration=2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.src))

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe(self):
        p = modes.probe(self.src)
        self.assertAlmostEqual(p["seconds"], 2.0, delta=0.05)
        self.assertEqual(p["fps"], 24)
        self.assertTrue(p["has_audio"])
        bad = self.d / "bad.mp4"
        bad.write_text("not a video")
        with self.assertRaises(Exception):
            modes.probe(bad)

    def test_pad(self):
        dst = self.d / "pad.mp4"
        subprocess.run(modes.pad_cmd(self.src, dst, 56), check=True, capture_output=True)
        self.assertEqual(count_frames(dst), 64)
        self.assertTrue(modes.probe(dst)["has_audio"])

    def test_trim_muxes_source_audio(self):
        gen = self.d / "gen.mp4"  # silent and longer, like a generated clip
        ff("-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=24", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(gen))
        dst = self.d / "out.mp4"
        subprocess.run(modes.trim_cmd(gen, self.src, dst, 2.0), check=True, capture_output=True)
        p = modes.probe(dst)
        self.assertAlmostEqual(p["seconds"], 2.0, delta=1 / 24)
        self.assertTrue(p["has_audio"])


class GeminiPrompt(unittest.TestCase):
    REPLY = ("Gemini said\n\nsubject_definitions:\n‹Video 1› is the source.\npin217.jpg\n+ 1\n\nsummary:\nx\n\n"
             "retention_analysis:\nx\n\ndetailed_description:\n‹Subject 1› (S1) is speaking, ‹d›[unclear]‹/d›.\n\n"
             "overall_soundscape:\nx\n\nnon_diegetic_music:\nN/A")

    def test_tags_back_and_chips_gone(self):
        got = modes.clean_prompt(self.REPLY)
        self.assertTrue(got.startswith("subject_definitions:\n<Video 1> is the source."))
        self.assertIn("<d>[unclear]</d>", got)
        self.assertNotIn("pin217", got)
        self.assertNotIn("+ 1", got)

    def test_cut_reply_is_rejected(self):
        self.assertIsNone(modes.clean_prompt("Gemini said\n\nsubject_definitions:"))

    def test_duration_filled(self):
        req = modes.prompt_request("up to {{duration_seconds}} s", "x", "", 12.25, [], [], "")
        self.assertIn("up to 12.25 s", req)


if __name__ == "__main__":
    unittest.main()
