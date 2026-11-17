# One WanGP generation in its own process (core of spike/run.py): python runner.py <run.json>
# run.json = {"model", "settings" (overrides) or "tasks" (list of overrides, one model load), "output_dir", "cli_args"}.
# Prints PROGRESS lines and one RESULT line per task.
import json, os, sys
from pathlib import Path

ROOT = Path(os.environ.get("WANGP_DIR", r"C:\AI\WanGP"))
sys.path.insert(0, str(ROOT))
from shared.api import init  # noqa: E402

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
session = init(root=ROOT, output_dir=Path(spec["output_dir"]), cli_args=spec["cli_args"])
tasks = spec.get("tasks") or [spec["settings"]]
for n, overrides in enumerate(tasks, 1):
    settings = session.get_default_settings(spec["model"])
    settings.update(overrides)
    job = session.submit_task(settings)
    last, part = None, f"part {n}/{len(tasks)} " if len(tasks) > 1 else ""
    for ev in job.events.iter(timeout=0.5):
        if ev.kind == "progress":
            p = ev.data
            if (p.phase, p.current_step) != last:
                last = (p.phase, p.current_step)
                print(f"PROGRESS {part}{p.phase} {p.current_step}/{p.total_steps}", flush=True)
    res = job.result()
    print("RESULT", json.dumps({"ok": res.success, "files": [str(f) for f in (res.generated_files or [])],
                                "errors": [e.message for e in (res.errors or [])]}), flush=True)
    if not res.success:
        break
