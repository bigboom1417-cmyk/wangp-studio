"""Gemini through the official API (GEMINI_API_KEY): text replies and images, stdlib only. Used when no browser helper is set."""
import base64, json, mimetypes, os, urllib.request
from pathlib import Path

URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
TEXT_MODEL = os.environ.get("STUDIO_GEMINI_TEXT_MODEL", "gemini-2.5-flash")
IMAGE_MODEL = os.environ.get("STUDIO_GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")


def _parts(prompt, refs):
    parts = [{"text": prompt}]
    for ref in list(refs)[:10]:
        mime = mimetypes.guess_type(str(ref))[0] or "application/octet-stream"
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(Path(ref).read_bytes()).decode()}})
    return parts


def _call(model, prompt, refs, timeout=420):
    body = json.dumps({"contents": [{"role": "user", "parts": _parts(prompt, refs)}]}).encode()
    req = urllib.request.Request(URL.format(model), body, {"Content-Type": "application/json",
                                                          "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.load(r)
    return [p for c in res.get("candidates", []) for p in c.get("content", {}).get("parts", [])]


def ask(prompt, refs=()):
    return "".join(p.get("text", "") for p in _call(TEXT_MODEL, prompt, refs)).strip()


def image(prompt, out, refs=()):
    for p in _call(IMAGE_MODEL, prompt, refs):
        data = p.get("inline_data") or p.get("inlineData")
        if data:
            Path(out).write_bytes(base64.b64decode(data["data"]))
            return out
    raise RuntimeError("Gemini API returned no image")
