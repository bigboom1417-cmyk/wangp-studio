import json, subprocess, tempfile, unittest
from pathlib import Path

import manga

STORY = {"title": "الفارس", "genre": "fantasy", "characters": [{"name": "Omar", "look": "tall, black hair"},
                                                              {"name": "Lina", "look": "red scarf"}],
         "chapters": [{"title": "One", "pages": [
             {"panels": [{"scene": "Omar at the gate", "characters": ["Omar"], "dialogue": [{"who": "Omar", "text": "افتحوا الباب"}]},
                         {"scene": "Lina waves", "characters": ["Lina"], "dialogue": [], "caption": "Dawn"}]},
             {"panels": [{"scene": "both ride", "characters": ["Omar", "Lina"], "dialogue": [{"who": "Lina", "text": "هيا"}]}]}]}]}


class Manga(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.drawn = []

    def tearDown(self):
        self.tmp.cleanup()

    def paint(self, prompt, out, refs):
        self.drawn.append((prompt, [str(r) for r in refs]))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=white:s=64x96", "-frames:v", "1", str(out)], check=True)
        return out

    def ask(self, prompt, refs=()):
        if prompt.startswith("You write a manga"):
            return "Sure!\n```json\n" + json.dumps(STORY, ensure_ascii=False) + "\n```"
        return '[{"x":0.1,"y":0.1,"w":0.3,"h":0.1}]'  # one box only: the rest get the fallback column

    def test_whole_book(self):
        job = {"id": "b1", "prompt": "a knight story", "params": {"style": "bw", "lang": "ar", "chapters": 1, "pages": 2},
               "files": {}}
        cover = manga.run(job, self.d / "b1", self.paint, self.ask, print)
        book = manga.load(self.d / "b1")
        self.assertTrue(cover.exists())
        self.assertEqual(len(book["cast"]), 2)
        self.assertEqual(len(book["pages"]), 2)
        self.assertEqual([b["text"] for b in book["pages"][0]["bubbles"]], ["افتحوا الباب", "Dawn"])
        self.assertEqual(book["pages"][0]["bubbles"][1]["kind"], "caption")
        page2 = [d for d in self.drawn if "panels" in d[0]][1]
        self.assertEqual(len(page2[1]), 2)  # both cast sheets for the page where both appear
        self.assertIn("right-to-left", page2[0])
        self.assertIn("NO text", page2[0])

    def test_edit_keeps_history_and_bubbles(self):
        job = {"id": "b2", "prompt": "x", "params": {"style": "color", "lang": "en", "chapters": 1, "pages": 2}, "files": {}}
        manga.run(job, self.d / "b2", self.paint, self.ask, print)
        book = manga.load(self.d / "b2")
        before = book["pages"][1]["bubbles"]
        manga.draw_page(book, self.d / "b2", 1, self.paint, self.ask, print, edit="make it night")
        book = manga.load(self.d / "b2")
        self.assertEqual(len(book["pages"][1]["history"]), 1)
        self.assertNotEqual(book["pages"][1]["history"][0], book["pages"][1]["image"])
        self.assertEqual(book["pages"][1]["bubbles"], before)
        self.assertTrue((self.d / "b2" / book["pages"][1]["image"]).exists())

    def test_recreate(self):
        src = self.d / "src.png"
        self.paint("", src, [])
        job = {"id": "b3", "prompt": "My cast", "params": {"style": "bw", "lang": "ar", "chapters": 1, "pages": 1},
               "files": {"refs": [str(src)], "pagerefs": [str(src), str(src)]}}
        manga.run(job, self.d / "b3", self.paint, self.ask, print)
        book = manga.load(self.d / "b3")
        self.assertEqual(len(book["pages"]), 2)
        self.assertEqual(len(book["cast"]), 1)

    def test_bad_story(self):
        with self.assertRaises(ValueError):
            manga.parse_story("I can't help with that")

    def test_boxes_clamped_and_filled(self):
        got = manga.parse_boxes('[{"x":1.4,"y":-1,"w":0.01,"h":0.5}]', 2)
        self.assertEqual(got[0], {"x": 1.0, "y": 0.0, "w": 0.12, "h": 0.5})
        self.assertEqual(len(got), 2)


if __name__ == "__main__":
    unittest.main()
