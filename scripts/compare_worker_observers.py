"""Compare local observation routes through real offline desktop workers."""

import argparse
import json
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from parallel_computer_use import desktop
from parallel_computer_use.model_process import ManagedModel
from parallel_computer_use.orchestrator import BROWSER_OBSERVATION_MODES, OBSERVATION_MODES, TERMINAL, Orchestrator
from parallel_computer_use.providers import LlamaProvider


MODES = OBSERVATION_MODES
CORE_TASKS = ("catalog", "shipping", "canvas")
TASKS = (*CORE_TASKS, "filter", "editor_read")
WIDTH, HEIGHT = 800, 600
STYLE = """
body { margin:0; padding:28px; color:#17243a; background:#f4f7fc; font:20px system-ui }
h1 { margin:0 0 20px; font-size:32px } h2 { margin:0 0 12px; font-size:24px }
.columns { display:grid; grid-template-columns:1fr 1fr; gap:22px }
section { padding:20px; border:1px solid #a8b8cd; border-radius:8px; background:white }
li { margin:10px 0 }
button { width:100%; height:80px; border:0; border-radius:5px; color:white; background:#21468b;
         font:700 20px system-ui }
canvas { display:block; width:690px; height:230px }
input { display:block; margin-top:8px; padding:12px; width:280px; font:inherit }
#status { font-size:23px; font-weight:650 }
"""
READY = "requestAnimationFrame(() => requestAnimationFrame(() => document.title = 'Local catalog'));"
RECORD = r"""
let receipt = 0;
const statusLine = document.querySelector('#status');
function record(label, message) {
  statusLine.textContent = message;
  const link = document.createElement('a');
  link.download = `receipt-${++receipt}.txt`;
  const url = URL.createObjectURL(new Blob([label + '\n'], {type:'text/plain'}));
  link.href = url;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
"""


@dataclass(frozen=True)
class Fixture:
    html: str
    prompt: str
    answer: str
    receipt: str | None = None
    check_point: tuple[int, int] | None = None
    notes: bytes | None = None
    crop_point: tuple[int, int] | None = None


def page(title, body, script=""):
    return (f"<!doctype html><meta charset='utf-8'><title>Opening benchmark</title>"
            f"<style>{STYLE}</style><h1>{title}</h1>{body}<script>{script}\n{READY}</script>")


def fixture(task, seed):
    if task == "editor_read":
        code = str(7317462 + seed * 28019)
        return Fixture("", "Read the verification code in the open notes file. Return exactly the seven digits.",
                       code, notes=f"Verification code: {code}\n".encode())
    if task == "catalog":
        lamp, notebook = 21 + seed * 3, 9 + seed
        rows = (f"<ul><li>Desk lamp · Iris · ${lamp}</li><li>Travel mug · Cedar · $16</li>"
                f"<li>Notebook · Elm · ${notebook}</li><li>Notebook · Cedar · $11</li></ul>")
        return Fixture(page("Parts catalog", rows),
                       "How many dollars more does the Iris desk lamp cost than the Elm notebook? "
                       "Return the amount in digits as the final result.", str(lamp - notebook))
    if task == "shipping":
        sections = ("Shipping", "Billing") if seed % 2 else ("Billing", "Shipping")
        columns = "".join(f"<section><h2>{name}</h2><button data-name='{name}'>Apply</button></section>"
                          for name in sections)
        code = 437 + seed * 13
        body = f"<div class='columns'>{columns}</div><p id='status' role='status'>Nothing applied</p>"
        script = RECORD + f"""
document.querySelectorAll('button').forEach(button => button.onclick = () => {{
  const name = button.dataset.name;
  record(name, `${{name}} applied. Receipt {code}.`);
}});
"""
        return Fixture(page("Order preferences", body, script),
                       "Click Apply in the Shipping section once. Leave Billing untouched. "
                       "Then return the complete confirmation shown on the page.",
                       f"Shipping applied. Receipt {code}.", "Shipping", (205 if seed % 2 else 595, 220))
    if task == "canvas":
        names = ("CANCEL", "EXPORT", "MERGE") if seed % 2 else ("MERGE", "CANCEL", "EXPORT")
        code = 673 + seed * 11
        x_shift, y_shift = (17, 64) if seed > 2 else (0, 0)
        canvas = "<canvas width='690' height='230'></canvas>"
        if x_shift or y_shift:
            canvas = f"<div style='padding:{y_shift}px 0 0 {x_shift}px'>{canvas}</div>"
        body = canvas + "<p id='status' role='status'>Nothing selected</p>"
        script = RECORD + f"""
const canvas = document.querySelector('canvas'), ctx = canvas.getContext('2d');
const names = {json.dumps(names)}, colors = ['#843a12', '#19467c', '#25664e'];
ctx.fillStyle = '#e8eef8'; ctx.fillRect(0, 0, 690, 230);
ctx.font = 'bold 24px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
names.forEach((name, index) => {{
  ctx.fillStyle = colors[index]; ctx.fillRect(20 + index * 220, 56, 182, 111);
  ctx.fillStyle = 'white'; ctx.fillText(name, 111 + index * 220, 111);
}});
canvas.onclick = event => {{
  const index = Math.floor((event.offsetX - 20) / 220);
  const x = event.offsetX - 20 - index * 220, y = event.offsetY;
  if (index >= 0 && index < names.length && x >= 0 && x < 182 && y >= 56 && y < 167) {{
    const name = names[index];
    record(name, `${{name}} selected. Receipt {code}.`);
  }}
}};
"""
        return Fixture(page("Drawing tools", body, script),
                       "Click the canvas control labeled EXPORT once. Then return the complete "
                       "confirmation shown on the page.", f"EXPORT selected. Receipt {code}.", "EXPORT",
                       (139 + x_shift + 220 * names.index("EXPORT"), 233 + y_shift),
                       crop_point=(111 + 220 * names.index("EXPORT"), 111))
    price = 13 + seed * 4
    body = ("<label for='inventory'>Filter inventory</label><input id='inventory' type='search' "
            "placeholder='Filter inventory'><p id='status' role='status'>Enter a search term</p>")
    script = RECORD + f"""
const search = document.querySelector('input');
search.oninput = () => {{
  if (search.value.trim().toLowerCase() === 'cedar') record('Cedar', 'Travel mug · Cedar · ${price}');
  else statusLine.textContent = 'No matching inventory';
}};
"""
    return Fixture(page("Inventory search", body, script),
                   "Enter Cedar in the Filter inventory field. Report the price of the resulting travel mug "
                   "in digits as the final result.", str(price), "Cedar", (140, 170))


class TracedProvider:
    is_cloud = False
    supports_images = True

    def __init__(self, model, turn_limit):
        self.model = model
        self.turn_limit = turn_limit
        self.turns = []

    def decide_with_usage(self, prompt, screenshot, cancel, *, directives=None):
        if len(self.turns) >= self.turn_limit:
            raise RuntimeError("Benchmark decision limit reached")
        began = time.monotonic()
        turn = {"prompt_bytes": len(prompt.encode()), "image_bytes": len(screenshot or b""),
                "allowed_directives": directives}
        self.turns.append(turn)
        try:
            directive, usage = self.model.decide_with_usage(prompt, screenshot, cancel, directives=directives)
            turn["directive"] = directive
            turn["usage"] = asdict(usage) if usage is not None else None
            return directive, usage
        except Exception as error:
            turn["error"] = f"{type(error).__name__}: {error}"
            usage = getattr(error, "usage", None)
            turn["usage"] = asdict(usage) if usage is not None else None
            raise
        finally:
            turn["ms"] = round((time.monotonic() - began) * 1000, 2)


def run_trial(model, mode, task, seed, *, timeout, turn_limit, pointer_units=None):
    sample = fixture(task, seed)
    tracked = TracedProvider(model, turn_limit)
    selected_template = "editor" if sample.notes is not None else "browser"

    def new_desktop(template, workspace, **options):
        if sample.notes is not None:
            with (workspace / "notes.txt").open("xb") as file:
                file.write(sample.notes)
        return desktop.DesktopSession(template, workspace=workspace, width=WIDTH, height=HEIGHT,
                                      browser_observer=mode in BROWSER_OBSERVATION_MODES, **options)

    def which(name, *, path=None):
        return None if name in ("firefox", "firefox-esr") else shutil.which(name, path=path)

    with (tempfile.TemporaryDirectory(prefix="pcu-worker-observer-") as state,
          patch.object(desktop, "_CATALOG", sample.html or desktop._CATALOG),
          patch.object(desktop, "shutil", SimpleNamespace(which=which))):
        app = Orchestrator(Path(state), new_desktop, {"local": tracked}, sessions=1, inference=1,
                           memory_floor=0, observation_mode=mode, model_pointer_units=pointer_units)
        try:
            began = time.monotonic()
            run_id = app.start_run({"prompt": sample.prompt, "template": selected_template,
                                    "observe_only": sample.receipt is None})
            with app.cv:
                job = app.runs[run_id].jobs[0]
                finished = app.cv.wait_for(lambda: job.status in TERMINAL or job.status == "awaiting_input", timeout)
                status, detail, result = job.status, job.detail, job.result
                wall_ms = round((time.monotonic() - began) * 1000, 2)
                if status not in TERMINAL:
                    app.cancel_run(run_id)
                    app.cv.wait_for(lambda: job.status in TERMINAL, 6)
            receipts = {name: app.artifact(job.id, name, limit=512).decode(errors="replace")
                        for name in job.artifacts if name.startswith("receipt-") and name.endswith(".txt")}
            expected = {} if sample.receipt is None else {"receipt-1.txt": sample.receipt + "\n"}
            state_correct = receipts == expected
            if sample.notes is not None:
                state_correct = (state_correct and "notes.txt" in job.artifacts
                                 and app.artifact(job.id, "notes.txt", limit=512) == sample.notes)
            answer = " ".join(result.split())
            if task in ("catalog", "filter"):
                answer = answer.removeprefix("$")
            answer_correct = answer == sample.answer
            _, snapshot = app.state()
            events = next(run["events"] for run in snapshot["runs"] if run["id"] == run_id)
            return {"mode": mode, "task": task, "seed": seed, "correct": status == "completed" and state_correct and answer_correct,
                    "state_correct": state_correct, "answer_correct": answer_correct, "timed_out": not finished,
                    "status": status, "detail": detail, "wall_ms": wall_ms, "expected_result": sample.answer,
                    "result": result, "expected_receipts": expected, "receipts": receipts, "turns": tracked.turns,
                    "model_usage": job.model_usage, "events": list(reversed(events))}
        finally:
            app.close()


class FixtureDriver:
    """Supply known fixture actions to check the worker and scorer without a model."""

    def __init__(self, task, seed):
        sample = fixture(task, seed)
        self.actions = []
        self.native_code = sample.answer if sample.notes is not None else None
        self.crop_point = sample.crop_point
        self.reference_label = {"shipping": "button Apply (Shipping)", "filter": "input Filter inventory"}.get(task)
        if sample.check_point:
            x, y = sample.check_point
            self.actions.append({"type": "click", "units": "pixels", "x": x, "y": y})
            if task == "filter":
                self.actions.append({"type": "type", "text": "Cedar"})
            self.actions.append({"type": "wait", "seconds": 1})
        self.actions.append({"type": "done", "result": sample.answer})

    def decide_with_usage(self, prompt, screenshot, cancel, *, directives=None):
        if self.native_code and screenshot is None and self.native_code not in prompt:
            raise RuntimeError("Text observation did not include the editor code")
        if self.actions[0]["type"] == "click":
            if self.reference_label and "click_ref" in directives:
                pattern = rf"^{re.escape(self.reference_label)}(?: \[focused\])? \[(e[1-9][0-9]?)\]"
                if match := re.search(pattern, prompt, re.MULTILINE):
                    self.actions.pop(0)
                    return {"type": "click_ref", "target": match.group(1)}, None
            if screenshot is None:
                if "visual" in directives:
                    return {"type": "visual"}, None
                raise RuntimeError("Fixture has no current input target")
            if self.crop_point and "This image is a crop." in prompt:
                x, y = self.crop_point
                return {**self.actions.pop(0), "x": x, "y": y}, None
        return self.actions.pop(0), None


def print_row(kind, row):
    print(json.dumps({"kind": kind, **row}, ensure_ascii=False), flush=True)


def summarize(rows, mode):
    tasks = {}
    for task in dict.fromkeys(row["task"] for row in rows):
        items = [row for row in rows if row["task"] == task]
        tasks[task] = {"correct": sum(row["correct"] for row in items), "trials": len(items)}
    usage = {}
    for row in rows:
        for key, value in row["model_usage"].items():
            usage[key] = usage.get(key, 0) + value
    return {"mode": mode, "correct": sum(row["correct"] for row in rows), "trials": len(rows),
            "median_wall_ms": round(statistics.median(row["wall_ms"] for row in rows), 2),
            "tasks": tasks, "model_usage": usage}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("server", "gguf", "projector"):
        parser.add_argument(f"--{field}", type=Path)
    parser.add_argument("--check", action="store_true", help="Check fixture effects with scripted input and no model")
    parser.add_argument("--mode", choices=MODES, nargs="+", default=MODES)
    parser.add_argument("--task", choices=TASKS, nargs="+", default=CORE_TASKS)
    parser.add_argument("--seeds", type=int, nargs="+", choices=range(1, 9), default=[1, 2])
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--gpu-layers", type=int, default=99)
    parser.add_argument("--timeout", type=int, choices=range(10, 181), default=65, metavar="SECONDS")
    parser.add_argument("--turn-limit", type=int, choices=range(2, 17), default=8)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--pointer-units", choices=("pixels", "normalized"))
    parser.add_argument("--no-warmup", action="store_true")
    args = parser.parse_args()
    if not args.check and not (args.server and args.gguf and args.projector):
        parser.error("--server, --gguf and --projector are required unless using --check")
    if args.check and args.pointer_units == "normalized":
        parser.error("--check uses fixture commands in pixel coordinates")
    browser_tasks = any(task != "editor_read" for task in args.task)
    version = None
    if browser_tasks:
        chromium = (shutil.which("chromium", path="/usr/bin:/bin") or shutil.which("chromium-browser", path="/usr/bin:/bin")
                    or str(desktop._CHROME))
        if not Path(chromium).is_file():
            parser.error("A Chromium installation is required for the browser trials")
        version = subprocess.run([chromium, "--version"], capture_output=True, text=True, check=True, timeout=4).stdout.strip()
    modes = list(dict.fromkeys(args.mode))
    print_row("configuration", {"browser": version, "viewport": [WIDTH, HEIGHT], "modes": modes,
                                "tasks": args.task, "seeds": args.seeds, "turn_limit": args.turn_limit,
                                "trial_timeout_s": args.timeout, "prompt_cache": not args.no_cache,
                                "check": args.check, "gguf": args.gguf.name if args.gguf else None,
                                "projector": args.projector.name if args.projector else None,
                                "gpu_layers": None if args.check else args.gpu_layers,
                                "pointer_units": args.pointer_units})
    options = {"timeout": args.timeout, "turn_limit": args.turn_limit, "pointer_units": args.pointer_units}
    if args.check:
        checks = []
        for seed, task, mode in product(dict.fromkeys(args.seeds), dict.fromkeys(args.task), modes):
            row = run_trial(FixtureDriver(task, seed), mode, task, seed, **options)
            checks.append(row["correct"])
            print_row("check", row)
        if not all(checks):
            raise SystemExit(1)
        return

    managed = ManagedModel(str(args.server.expanduser()), args.gguf, args.projector, slots=1, context_per_slot=6144,
                           gpu_layers=args.gpu_layers, threads=2, gpu_device=args.gpu if args.gpu_layers else None)
    if args.no_cache:
        managed.options.append("--no-cache-prompt")
    results = []
    with managed as server:
        model = LlamaProvider(server.alias, server.base_url, api_key=server.api_key,
                              reasoning_budget=96, max_output_tokens=384, timeout=min(args.timeout, 30))
        if not args.no_warmup:
            print_row("warmup", run_trial(model, "pixels", "catalog" if browser_tasks else "editor_read", 9, **options))
        for index, (seed, task) in enumerate(product(dict.fromkeys(args.seeds), dict.fromkeys(args.task))):
            shift = index % len(modes)
            for mode in (*modes[shift:], *modes[:shift]):
                row = run_trial(model, mode, task, seed, **options)
                results.append(row)
                print_row("trial", row)
    for mode in modes:
        print_row("summary", summarize([row for row in results if row["mode"] == mode], mode))


if __name__ == "__main__":
    main()
