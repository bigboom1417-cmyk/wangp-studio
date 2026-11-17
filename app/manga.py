"""Mangaka: idea -> story (Gemini text) -> cast sheets -> pages (Gemini images, no lettering) -> bubble boxes (Gemini vision).
A book lives in library/manga/<job id>/book.json with its pictures; the page letters the bubbles in the browser (Arabic RTL)."""
import json, re, shutil
from pathlib import Path

STYLES = {"bw": "black-and-white Japanese manga, screentone shading, ink lines",
          "color": "full-colour Japanese manga, clean cel shading",
          "comic": "western comic book, bold inks, flat colours",
          "webtoon": "full-colour Korean webtoon, soft shading"}
LANGS = {"ar": "Arabic", "en": "English", "ja": "Japanese", "fr": "French", "es": "Spanish", "tr": "Turkish"}
MAX_CAST_REFS = 9  # Gemini takes 10 files per message: the cast sheets + one more


def story_request(idea, style, lang, chapters, pages, n_refs):
    pics = (f" The {n_refs} attached picture(s) are the main characters, in order: describe their looks from the pictures."
            if n_refs else "")
    return f"""You write a manga. Idea or script: {idea}
Style: {STYLES[style]}. Dialogue and captions in {LANGS[lang]}. {chapters} chapter(s), {pages} page(s) per chapter, 3 to 5 panels per page.{pics}
Return ONLY one JSON object, no Markdown, in this shape:
{{"title": "...", "genre": "...", "logline": "...",
 "characters": [{{"name": "...", "look": "face, hair, build, age, outfit down to the shoes, colours"}}],
 "locations": [{{"name": "...", "look": "..."}}],
 "props": [{{"name": "...", "look": "..."}}],
 "chapters": [{{"title": "...", "pages": [{{"panels": [{{"scene": "what the panel shows, camera framing", "characters": ["names"],
   "location": "name", "dialogue": [{{"who": "name", "text": "line"}}], "caption": ""}}]}}]}}]}}
Keep each line short enough for a speech bubble. Names and looks stay the same everywhere."""


def parse_story(text):
    """The story dict out of a Gemini reply; raises ValueError when it is not usable."""
    m = re.search(r"\{.*\}", text.replace("```json", "").replace("```", ""), re.S)
    if not m:
        raise ValueError("Gemini returned no story")
    story = json.loads(m.group(0))
    if not story.get("characters") or not story.get("chapters") or not all(c.get("pages") for c in story["chapters"]):
        raise ValueError("the story has no characters or pages")
    return story


def sheet_prompt(style, ch, has_ref):
    ref = "Picture 1 shows this person: keep their face and build. " if has_ref else ""
    return (f"{ref}Character sheet in {STYLES[style]} style: {ch['name']}, {ch['look']}. Full body front view, side view "
            "and a face close-up side by side on a plain white background. No text, no labels.")


def page_prompt(style, page, cast_names, rtl):
    order = "right-to-left like Japanese manga" if rtl else "left-to-right"
    shape = "one tall vertical webtoon strip, panels stacked top to bottom" if style == "webtoon" else "one portrait comic page"
    pics = " ".join(f"Picture {i + 1} is the character sheet of {n}." for i, n in enumerate(cast_names))
    panels = " ".join(f"Panel {i + 1}: {p['scene']}" + (f" ({', '.join(p.get('characters') or [])})" if p.get("characters") else "")
                      + (f", at {p['location']}" if p.get("location") else "") + "." for i, p in enumerate(page["panels"]))
    return (f"{pics} Draw {shape} in {STYLES[style]} style with {len(page['panels'])} panels, read {order}. {panels} "
            "Every character looks exactly like their sheet. Leave empty space near the speakers for speech bubbles, "
            "but draw NO speech bubbles, NO text, NO letters and NO sound effects.")


def page_lines(page):
    out = [{"who": d["who"], "text": d["text"], "kind": "speech", "panel": i}
           for i, p in enumerate(page["panels"]) for d in (p.get("dialogue") or [])]
    return out + [{"who": "", "text": p["caption"], "kind": "caption", "panel": i}
                  for i, p in enumerate(page["panels"]) if p.get("caption")]


def bubble_request(page, rtl):
    lines = [f"{k + 1}. panel {l['panel'] + 1}, " + (f"{l['who']} says: " if l["kind"] == "speech" else "caption box: ") + l["text"]
             for k, l in enumerate(page_lines(page))]
    order = "right to left, top to bottom" if rtl else "left to right, top to bottom"
    return (f"This comic page has {len(page['panels'])} panels, numbered in reading order ({order}). Place one box per line "
            "below: a speech bubble over empty space next to the speaker's head in that panel, never over a face; a caption "
            "box in a top corner of that panel. If the drawing already has an empty speech bubble near that speaker, use "
            "exactly its box. Spread the lines over their own panels. "
            "Return ONLY a JSON list, one object per line in order: {\"x\":0.1,\"y\":0.05,\"w\":0.3,\"h\":0.12} with x, y = the "
            "top-left corner and w, h = the size, all as fractions of the page width and height.\n" + "\n".join(lines))


def parse_boxes(text, n):
    """n boxes from a Gemini reply, filling gaps with a column down the page so every line still gets a bubble."""
    boxes = []
    m = re.search(r"\[.*\]", text or "", re.S)
    if m:
        try:
            boxes = [b for b in json.loads(m.group(0)) if isinstance(b, dict)]
        except ValueError:
            boxes = []
    out = []
    for i in range(n):
        b = boxes[i] if i < len(boxes) else {}
        try:
            x, y, w, h = (min(max(float(b[k]), 0.0), 1.0) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            x, y, w, h = 0.62, 0.04 + (i % 8) * 0.12, 0.34, 0.1
        out.append({"x": x, "y": y, "w": max(w, 0.12), "h": max(h, 0.06)})
    return out


def run(job, book_dir, run_image, ask, on_progress):
    """Mode manga: the whole book. Returns the cover picture (the first page)."""
    p, f, d = job["params"], job["files"], Path(book_dir)
    d.mkdir(parents=True, exist_ok=True)
    style, lang, refs = p["style"], p["lang"], f.get("refs", [])
    book = {"id": job["id"], "style": style, "lang": lang, "pages": [], "cast": []}
    if f.get("pagerefs"):  # Recreate: his manga pages redrawn with his cast, their own lettering kept
        book["title"] = job["prompt"][:80]
        for i, ref in enumerate(refs):
            book["cast"].append({"name": f"Cast {i + 1}", "look": "", "sheet": Path(shutil.copy(ref, d / f"cast_{i}{Path(ref).suffix}")).name})
        for i, src in enumerate(f["pagerefs"]):
            on_progress(f"Gemini redraws page {i + 1}/{len(f['pagerefs'])}")
            out = d / f"page_{i}.png"
            run_image(f"Picture 1 is a comic page. Redraw it exactly: same panels, poses, framing and lettering, in the same style, "
                      f"but the characters become the people in the other pictures, in order of who appears most. {job['prompt']}",
                      out, [src] + refs[:MAX_CAST_REFS])
            book["pages"].append({"chapter": 0, "image": out.name, "bubbles": []})
        _save(d, book)
        return d / book["pages"][0]["image"]
    on_progress("Gemini writes the story")
    story = parse_story(ask(story_request(job["prompt"], style, lang, p["chapters"], p["pages"], len(refs)), refs))
    book.update({k: story.get(k, "") for k in ("title", "genre", "logline", "locations", "props")})
    book["chapters"] = [c.get("title", "") for c in story["chapters"]]
    for i, ch in enumerate(story["characters"]):
        on_progress(f"Gemini draws {ch['name']} ({i + 1}/{len(story['characters'])})")
        out = d / f"cast_{i}.png"
        ref = [refs[i]] if i < len(refs) else []
        run_image(sheet_prompt(style, ch, bool(ref)), out, ref)
        book["cast"].append({**ch, "sheet": out.name})
    _save(d, book)
    pages = [(ci, pg) for ci, c in enumerate(story["chapters"]) for pg in c["pages"]]
    book["pages"] = [{"chapter": ci, "panels": pg["panels"], "image": "", "bubbles": []} for ci, pg in pages]
    for n in range(len(pages)):
        draw_page(book, d, n, run_image, ask, on_progress)
    return d / book["pages"][0]["image"]


def draw_page(book, d, n, run_image, ask, on_progress, edit=""):
    """(Re)draws page n, or edits it with a prompt, then places its bubbles. Saves the book."""
    page = book["pages"][n]
    rtl = book["lang"] in ("ar", "ja") or book["style"] == "bw"  # manga and Arabic read right to left
    out = d / f"page_{n}_{len(page.get('history', [])) + bool(page.get('image'))}.png"  # a new file per version
    if edit:
        on_progress(f"Gemini edits page {n + 1}")
        run_image(f"Edit this comic page: {edit}. Keep everything else exactly the same. Draw no text.", out, [d / page["image"]])
    else:
        on_progress(f"Gemini draws page {n + 1}/{len(book['pages'])}")
        names = {c for p in page.get("panels", []) for c in (p.get("characters") or [])}
        cast = [c for c in book["cast"] if c["name"] in names][:MAX_CAST_REFS] or book["cast"][:MAX_CAST_REFS]
        run_image(page_prompt(book["style"], page, [c["name"] for c in cast], rtl), out, [d / c["sheet"] for c in cast])
    if page.get("image"):
        page.setdefault("history", []).append(page["image"])
    page["image"] = out.name
    lines = page_lines(page) if page.get("panels") else []
    if lines and not edit:
        on_progress(f"Gemini places bubbles on page {n + 1}")
        boxes = parse_boxes(ask(bubble_request(page, rtl), [out]), len(lines))
        page["bubbles"] = [{**l, **b} for l, b in zip(lines, boxes)]
    _save(d, book)


def _save(d, book):
    tmp = d / "book.tmp"
    tmp.write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "book.json")


def load(d):
    return json.loads((Path(d) / "book.json").read_text(encoding="utf-8"))
