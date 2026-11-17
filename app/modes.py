"""Pure job -> WanGP settings, plus the ffmpeg steps around a video job."""
import json, math, re, subprocess

RES = {("9:16", 480): "480x832", ("16:9", 480): "832x480", ("1:1", 480): "624x624",
       ("9:16", 720): "720x1280", ("16:9", 720): "1280x720", ("1:1", 720): "960x960"}
H3 = "minimax_h3_fl2va_hyperflow"
REF = "minimax_h3_ref2va_hyperflow"
WINDOW = 481  # frames per sliding window (20 s, the model max; 30 s ran in 655 s on a 12 GB RTX card); longer clips = several windows in one job
VIDS = {1: "V-U", 2: "V+-U", 3: "V+*-U"}  # Ref2VA reference-video choices (handler guide_custom_choices)


def frames_for(seconds):
    need = math.ceil(round(seconds * 24, 6))  # round: 5.8*24 must not become 139.20000001
    return 17 * max(0, math.ceil((need - 5) / 17)) + 5


def seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out and out != "N/A" else 0.0


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,r_frame_rate,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    video = [s for s in info.get("streams", []) if s["codec_type"] == "video"]
    if not video:
        raise ValueError("no video stream")
    num, den = video[0]["r_frame_rate"].split("/")
    return {"seconds": float(info["format"]["duration"]), "fps": int(num) / int(den),
            "size": (int(video[0]["width"]), int(video[0]["height"])),
            "has_audio": any(s["codec_type"] == "audio" for s in info["streams"])}


ENC = ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac"]


def pad_cmd(src, dst, frames):
    # 8 frames past the output so the model never invents the last second (spike rule)
    total = frames + 8
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", "fps=24,tpad=stop_mode=clone:stop=-1",
            "-af", f"apad=whole_dur={total / 24}", "-frames:v", str(total), *ENC, str(dst)]


def trim_cmd(gen, src, dst, seconds):
    if probe(gen)["has_audio"]:
        inputs = ["-i", str(gen)]
    else:
        inputs = ["-i", str(gen), "-i", str(src), "-map", "0:v", "-map", "1:a?"]
    return ["ffmpeg", "-v", "error", "-y", *inputs, "-t", f"{seconds:.3f}", *ENC, str(dst)]


def settings(job):
    s = {"config": "distilled_4b", "resolution": RES[(job["ratio"], int(job["quality"]))],
         "prompt": job["prompt"], "video_length": job["frames"],
         "seed": 42 if job.get("seed") is None else job["seed"]}
    mode = job["mode"]
    if mode == "create":
        model = H3
        if job.get("start"):
            s["image_start"] = [job["start"]]
        if job.get("end"):
            s["image_end"] = [job["end"]]
    elif mode == "restyle":
        model = H3
        s.update(video_prompt_type="GV", denoising_strength=job["strength"], audio_prompt_type="KS", video_guide=job["source"])
    elif mode == "refs":
        model = REF
        imgs, vids, auds = job.get("refs") or [], job.get("vrefs") or [], job.get("arefs") or []
        s.update(video_prompt_type=("I" if imgs else "") + VIDS.get(len(vids), ""), audio_prompt_type="ABD"[:len(auds)])
        if imgs:
            s["image_refs"] = imgs
        for key, files in (("video_guide", vids), ("audio_guide", auds)):
            for i, path in enumerate(files):
                s[key + (str(i + 1) if i else "")] = path
    elif mode == "look":
        model = "viggle_remake"
        s.update(multi_prompts_gen_type="FG", video_prompt_type="FIVU", frames_positions=f"1 {job['frames']}",
                 image_refs=[job["start"], job["end"]], video_guide=job["source"], audio_prompt_type="K",
                 sliding_window_size=job["frames"])
    else:
        raise ValueError(f"unknown mode {mode}")
    if mode != "look":
        if job["frames"] > WINDOW:
            s["sliding_window_size"] = WINDOW
        # a blank line starts the next part of a long clip; one paragraph = same prompt for every part
        s["multi_prompts_gen_type"] = "PW" if len([p for p in job["prompt"].split("\n\n") if p.strip()]) > 1 else "FG"
    if job.get("smooth"):
        s["temporal_upsampling"] = "rife*2"
    return model, s


LOOK = """subject_definitions:
<Video 1> is the source video for the editing task: one short single take of {src}.
<Picture 1> and <Picture 2> are frames of the target video, <Picture 1> from [Shot 1] at 00:00.000 and <Picture 2> from [Shot 1] at {end}.
<Subject 1> is the person in <Picture 1> and <Picture 2>: {look}. They replace {src} in <Video 1>.

summary:
[video editing + character replacement] The target video is an edited version of <Video 1> in which {src} becomes <Subject 1>, looking exactly as in <Picture 1>. The same head, mouth and hand movements are kept. The shot, camera, motion and timing of <Video 1> are kept.

retention_analysis:
<Video 1> (source video editing): fully_preserved - the single shot with no cut, the camera framing, and the motion and timing of every head turn, mouth movement and hand gesture.
<Picture 1> (appears in [Shot 1]): reference - a frame of the target video. The person in it, their face and clothing, and the background, framing and lighting are taken from <Picture 1>.
<Picture 2> (appears in [Shot 1]): reference - a frame of the target video at {end}.
<Subject 1> (appears in [Shot 1]): fully_preserved - their face, hair and clothing stay identical in every frame.

detailed_description:
Vivid cinematic footage.
[Shot 1] A shot of <Subject 1> matching <Picture 1> at 00:00.000 and <Picture 2> at {end}. From 00:00.000 <Subject 1> (S1) moves and speaks exactly as in <Video 1>, with natural lip movements synchronized, <d>[unclear]</d>. In every frame they look exactly as in <Picture 1>; {src} does not appear at any moment.

overall_soundscape:
The soundtrack of <Video 1>, unchanged.

non_diegetic_music:
N/A
"""


def look_prompt(look, source_desc, seconds):
    return LOOK.format(look=look, src=source_desc or "the person in the source video", end=f"00:{seconds:06.3f}")


# ---- Genjutsu: Viggle Remake swap/edit over WanGP's own window plan (design docs/2026-11-17-genjutsu-mangaka-design.md)
VIGGLE = {"swap": "viggle_remake", "edit": "viggle_remake_edit"}
VSIZE = {("16:9", 480): (832, 480), ("16:9", 720): (1280, 736), ("9:16", 480): (480, 832), ("9:16", 720): (736, 1280),
         ("1:1", 480): (624, 624), ("1:1", 720): (960, 960)}  # Viggle Remake: 832x480 drafts, 1280x736 finals


def ratio_of(w, h):
    return "16:9" if w > h * 1.2 else "9:16" if h > w * 1.2 else "1:1"


def lite_question(desc):
    return ("Reply with one word only, swap or edit. swap = the request changes who a person or character is "
            "(face, body, outfit). edit = it changes objects, the place, the weather, the light or the art style while "
            f"the people stay the same.\nRequest: {desc}")


def paint_instruction(mode, desc, style, n_refs, has_first, t=None):
    # the time lets a request that changes over the clip ("startled at 3 s") pick the right moment
    pics = ["Picture 1 is one frame of a video" + (f", at {t:.1f} s." if t is not None else ".")]
    nxt = 2
    if has_first:
        pics.append(f"Picture {nxt} is an earlier frame of the same video that is already finished: the new characters "
                    "and things must look exactly as they do there.")
        nxt += 1
    if n_refs:
        last = nxt + n_refs - 1
        pics.append(f"Picture{'s' if n_refs > 1 else ''} {nxt}{f' to {last}' if n_refs > 1 else ''} show what the new "
                    "characters, clothes, objects or place must look like.")
    keep = ("every person keeps the exact same pose, hand positions, head angle and mouth shape" if mode == "swap" else
            "every person stays exactly as they are: same face, clothes, pose and expression")
    return (" ".join(pics) + f" Redraw Picture 1 with this change: {desc}. Keep the same image size, framing, camera "
            f"angle and composition; {keep}; anything the change does not touch stays the same, with matching light "
            "and shadows. " + (f"Draw the whole picture in this style: {style}. " if style else
                               "Keep it photorealistic like the original. ") +
            "No borders, no text, no cropping. Return only the picture.")


def window_pictures(pins_rel, k, windows):
    """(picture lines for the prompt writer, [pin index] for the attached pictures) of window k. Times are window time."""
    from pins import HOP, window_of
    lines, idx = [], []
    n = 1
    if k:
        lines.append("<Picture 1> is the start image of this part: the frame continued from the previous part of the take.")
        n = 2
    for i, p in enumerate(pins_rel):
        if window_of(p, windows) == k:  # written as the model reads it, so the fallback prompt can use it as is
            t = (p - k * HOP) / 24
            lines.append(f"<Picture {n}> is a frame of the target video, from [Shot 1] at 00:{t:06.3f}.")
            idx.append(i)
            n += 1
    return lines, idx


def prompt_request(rules, desc, style, duration, lines, cuts_s, src_desc):
    rules = rules.replace("{{duration_seconds}}", f"{duration:.2f}").replace("{duration_seconds}", f"{duration:.2f}")
    shots = (f"The source has cuts at {', '.join(f'{c:.2f}s' for c in cuts_s)} (window time)." if cuts_s
             else "The source is one continuous shot.")
    return (f"{rules}\n\nRequest: {desc}" + (f" Style: {style}." if style else "") +
            " Whoever talks in the source keeps talking: mark them as speaking with natural lip movements synchronized." +
            (f"\nThe source video shows: {src_desc}." if src_desc else "") +
            f"\n{shots}\nThe pictures of this part (the attached images are its injected frames, in this order):\n" + "\n".join(lines) +
            f"\nThe video lasts {duration:.2f} s. Write the prompt now: only the six sections, no Markdown, no code block. "
            "IMPORTANT: your chat window deletes angle brackets, so write every tag with ‹ and › instead: "
            "‹Video 1›, ‹Picture 1›, ‹Subject 1›, ‹d›[unclear]‹/d›.")


SECTIONS = ("subject_definitions:", "summary:", "retention_analysis:", "detailed_description:",
            "overall_soundscape:", "non_diegetic_music:")


def clean_prompt(text):
    """The six sections out of a Gemini reply, or None when they are not all there."""
    text = text.replace("```", "").replace("‹", "<").replace("›", ">")  # Gemini's page eats real <tags>
    # its attachment chips ("pin217.jpg", "+ 1") land inside the copied text
    text = "\n".join(l.rstrip() for l in text.splitlines()
                     if not re.fullmatch(r"\s*(\S+\.(png|jpe?g|webp)|\+ \d+)\s*", l, re.I))
    i = text.find(SECTIONS[0])
    if i < 0 or not all(s in text for s in SECTIONS):
        return None
    return text[i:].strip().replace("dimly", "low-light").replace(" dim ", " dark ")


def fallback_prompt(mode, desc, lines, duration):
    pics = " ".join(lines)
    end = f"00:{duration:06.3f}"
    if mode == "swap":
        return f"""subject_definitions:
<Video 1> is the source video for the editing task.
{pics}
<Subject 1> is the new look shown in the painted pictures: {desc}.

summary:
[video editing + character replacement] The target video is an edited version of <Video 1> in which the people become exactly as the painted pictures show them: {desc}. The shots, cuts, camera, motion and timing of <Video 1> are kept.

retention_analysis:
<Video 1> (source video editing): fully_preserved - the shots and cuts, the camera framing and movement, the place, and the motion and timing of every performance.
<Subject 1> (appears in [Shot 1]): fully_preserved - their faces, hair and outfits stay identical in every frame.

detailed_description:
Vivid cinematic footage.
[Shot 1] From 00:00.000 to {end} the people move and speak exactly as in <Video 1>, with natural lip movements synchronized, <d>[unclear]</d>, looking exactly as in the painted pictures. Their original look appears in no frame.

overall_soundscape:
The soundtrack of <Video 1>, unchanged.

non_diegetic_music:
N/A"""
    return f"""subject_definitions:
<Video 1> is the source video for the target video edit.
{pics}
<Subject 1> is the change shown in the painted pictures: {desc}.

summary:
[video editing] The target video is an edited version of <Video 1>: {desc}, with exactly the same framing, motion, light and timing as <Video 1>.

retention_analysis:
<Video 1> (source video editing): fully_preserved - the framing and camera movement, the people, and every part the change does not touch.
<Subject 1> (appears in [Shot 1]): new - {desc}.

detailed_description:
Vivid cinematic footage.
[Shot 1] From 00:00.000 to {end} everything moves exactly as in <Video 1>, matching the painted pictures at their times.

overall_soundscape:
The soundtrack of <Video 1>, unchanged.

non_diegetic_music:
N/A"""


def genjutsu_settings(mode, size, clip, group, pin_files, prompts, upscale):
    """WanGP overrides for one render group (one take, possibly several sliding windows)."""
    r0, r1 = group["render"]
    s = {"config": "distilled_4b", "resolution": f"{size[0]}x{size[1]}", "video_length": r1 - r0, "seed": 42,
         "video_prompt_type": "FIVU", "frames_positions": " ".join(str(p - r0 + 1) for p in group["pins"]),
         "image_refs": pin_files, "video_guide": clip, "audio_prompt_type": "K",
         "sliding_window_size": min(r1 - r0, 294) if group["windows"] > 1 else r1 - r0, "sliding_window_overlap": 17,
         "multi_prompts_gen_type": "PW" if len(prompts) > 1 else "FG", "prompt": prompts[0]}
    if len(prompts) > 1:  # PW splits windows on blank lines, so each window's sections lose theirs
        s["prompt"] = "\n\n".join("\n".join(l for l in p.splitlines() if l.strip()) for p in prompts)
    if upscale:
        s["spatial_upsampling"] = "relayvsr*2"
    return VIGGLE[mode], s


def take_filter(n, owns, renders, size):
    """ffmpeg filter that trims each take to the frames it owns and joins them."""
    parts = [f"[{i}:v]trim=start_frame={a - r}:end_frame={b - r},setpts=PTS-STARTPTS,scale={size[0]}:{size[1]}[v{i}]"
             for i, ((a, b), (r, _)) in enumerate(zip(owns, renders))]
    return ";".join(parts) + ";" + "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
