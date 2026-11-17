import base64, io, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

import gemini_api


def reply(parts):
    return io.BytesIO(json.dumps({"candidates": [{"content": {"parts": parts}}]}).encode())


@mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"})
class GeminiApiTest(unittest.TestCase):
    def test_ask_sends_prompt_and_refs_and_joins_text(self):
        with tempfile.TemporaryDirectory() as d:
            ref = Path(d, "a.png"); ref.write_bytes(b"png")
            with mock.patch("urllib.request.urlopen", return_value=reply([{"text": "hi "}, {"text": "there"}])) as op:
                self.assertEqual(gemini_api.ask("q", [ref]), "hi there")
        req = op.call_args[0][0]
        self.assertIn(gemini_api.TEXT_MODEL, req.full_url)
        self.assertEqual(req.get_header("X-goog-api-key"), "k")
        parts = json.loads(req.data)["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "q"})
        self.assertEqual(parts[1]["inline_data"], {"mime_type": "image/png", "data": base64.b64encode(b"png").decode()})

    def test_image_writes_first_inline_image(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d, "o.png")
            parts = [{"text": "here"}, {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"IMG").decode()}}]
            with mock.patch("urllib.request.urlopen", return_value=reply(parts)):
                gemini_api.image("p", out)
            self.assertEqual(out.read_bytes(), b"IMG")

    def test_image_without_image_raises(self):
        with mock.patch("urllib.request.urlopen", return_value=reply([{"text": "no"}])):
            self.assertRaises(RuntimeError, gemini_api.image, "p", "x.png")


if __name__ == "__main__":
    unittest.main()
