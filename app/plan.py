# Genjutsu plan, run with WanGP's python (cwd C:\AI\WanGP): python plan.py <src 24 fps> <swap|edit> [pin seconds, comma separated]
# Prints one JSON line: frames, cuts, render groups (WanGP's own window planner) with the frames to paint, and WanGP's prompt rules.
import json, os, sys

sys.path.insert(0, os.environ.get("WANGP_DIR", r"C:\AI\WanGP"))
import numpy as np  # noqa: E402
from shared.deepy import video_edit as ve  # noqa: E402
from models.minimax_h3.viggle import VIGGLE_REMAKE_ENHANCER_INSTRUCTIONS  # noqa: E402

import pins  # noqa: E402

src, mode = sys.argv[1], sys.argv[2]
asked = [round(float(s) * 24) for s in (sys.argv[3] if len(sys.argv) > 3 else "").split(",") if s.strip()]
det = ve.detect_cuts(src)
frames, cuts = det["frames"], det["cuts"]
groups = ve.render_groups(ve.plan_windows(cuts, frames), frames)
thumbs = ve.read_thumbnails(src).astype(np.float32).mean(-1)
sharp = (np.abs(np.diff(thumbs, axis=1)).mean((1, 2)) + np.abs(np.diff(thumbs, axis=2)).mean((1, 2))).tolist()
for g in groups:
    g["pins"] = pins.choose(g, cuts, sharp, asked)
print(json.dumps({"frames": frames, "cuts": cuts, "groups": groups,
                  "rules": VIGGLE_REMAKE_ENHANCER_INSTRUCTIONS[mode]}))
