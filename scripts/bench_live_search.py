"""Qualify three read-only Python documentation searches in isolated browser workers."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import urlopen
from urllib.robotparser import RobotFileParser

from parallel_computer_use._network import validate_network_target
from parallel_computer_use.desktop import DesktopSession
from parallel_computer_use.model_process import ManagedModel
from parallel_computer_use.orchestrator import BROWSER_INPUT, TERMINAL, Orchestrator
from parallel_computer_use.providers import LlamaProvider, ProviderResponseError


HOST = "docs.python.org"
ORIGIN = f"https://{HOST}"
DOCS = f"{ORIGIN}/3.14"
QUERIES = {
    "argparse": "argparse — Parser for command-line options, arguments and subcommands",
    "pathlib": "pathlib — Object-oriented filesystem paths",
    "dataclasses": "dataclasses — Data Classes",
}
VIEWPORT = (1280, 960)
LINK = re.compile(r"^a (.+?) \((https://[^\s()]+)\)(?: .*?)? \[(e[1-9]\d?)\]$")
MARKERS = {
    "access_challenge": re.compile(r"access denied|verify (?:that )?you are human|captcha|checking your browser|too many requests", re.I),
    "browser_error": re.compile(r"err_[a-z_]+|this site can[’']?t be reached|your connection is not private|could not connect", re.I),
    "search_empty": re.compile(r"your search did not match|no search results|no results found", re.I),
}


def clean_title(title):
    return " ".join(title.strip().strip('"`').replace("`", "").removesuffix("¶").split())


def docs_url(raw, base):
    if not isinstance(raw, str) or not raw or any(char.isspace() or char == "\\" for char in raw):
        return None
    try:
        url = urlsplit(urljoin(base, raw))
        if url.scheme != "https" or url.netloc != HOST:
            return None
    except ValueError:
        return None
    return f"{ORIGIN}{url.path}"


@dataclass
class Search:
    query: str
    started: float = 0.0
    ready: float = 0.0
    closed: float = 0.0
    pixel_verifier_ms: float = 0.0
    job: object = None
    requests: list = field(default_factory=list)
    pages: list = field(default_factory=list)

    @property
    def search_url(self):
        return f"{DOCS}/search.html?{urlencode({'q': self.query})}"

    @property
    def chapter(self):
        return f"{DOCS}/library/{self.query}.html"

    @property
    def title(self):
        return QUERIES[self.query]


def allowed_searches(searches, *, follow):
    robots_url = f"{ORIGIN}/robots.txt"
    with urlopen(robots_url, timeout=8) as response:
        if response.status != 200 or response.geturl() != robots_url:
            raise RuntimeError("Could not confirm the Python docs robots policy")
        raw = response.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise RuntimeError("Python docs robots policy exceeded the read limit")
    robots = RobotFileParser()
    robots.parse(raw.decode("utf-8").splitlines())
    pages = [search.search_url for search in searches] + [f"{DOCS}/searchindex.js"]
    if follow:
        pages.extend(search.chapter for search in searches)
    if any(not robots.can_fetch("*", url) for url in pages):
        raise RuntimeError("Python docs robots policy disallows a benchmark URL")


def targets(observation, search):
    for raw in observation.splitlines():
        match = LINK.fullmatch(raw)
        if match:
            title, url, ref = match.groups()
            if docs_url(url, search.search_url) == search.chapter:
                yield clean_title(title), ref


def observed_target(observation, search):
    return next((ref for title, ref in targets(observation, search) if title == search.title), None)


def observed_heading(observation, search):
    return any(line.startswith("h1 ") and clean_title(line[3:]) == search.title
               for line in observation.splitlines())


def answer_matches(result, search, title_only=False):
    if title_only:
        return clean_title(result) == search.title
    title, sep, url = result.partition("|")
    return bool(sep and clean_title(title) == search.title and url.strip() == search.chapter)


def assignment(search, follow, title_only, click_link=False):
    result = ('Return only the exact visible title in your done result.' if title_only else
              'Return the exact visible result title and its listed URL as "title | URL" in your done result.')
    read = (f"Find the Python standard-library module reference for {search.query} in the opened search results. "
            f"{result} "
            "Wait briefly if results are still loading. If the page explicitly shows a browser or access error, report blocked. "
            "Use only what the browser shows and never guess a URL or title.")
    if follow:
        click = ('Use click_link with the exact visible HTTPS URL as its "url" field.' if click_link else
                 "Use the element reference printed on that line.")
        read = (f"Open the standard-library {search.query} module reference. Click the visible search result whose "
                f"URL ends in /library/{search.query}.html. {click} "
                'Once the chapter appears, return done with its exact visible heading and current page URL '
                'separated by " | ". Base the result on the browser observation.')
    return {"title": search.query, "template": "browser", "start_url": search.search_url,
            "observe_only": not follow, "prompt": read}


class BenchDesktop(DesktopSession):
    def __init__(self, template, search, witness_pixels, **options):
        self.search = search
        self.witness_pixels = witness_pixels
        search.started = time.monotonic()
        super().__init__(template, allowed_domains=(HOST,), browser_observer=True,
                         width=VIEWPORT[0], height=VIEWPORT[1], **options)
        until = time.monotonic() + 2.5
        while not any(page["visible_target"] for page in search.pages) and time.monotonic() < until:
            self.browser_state()
            if not any(page["visible_target"] for page in search.pages):
                time.sleep(0.08)
        if any(page["visible_target"] for page in search.pages):
            self.browser_frame_ready()
        search.ready = time.monotonic()

    def browser_state(self):
        state = super().browser_state()
        if state:
            text = state["text"]
            self.search.pages.append({
                "at": time.monotonic(), "url": docs_url(state.get("page"), self.search.search_url),
                "visible_target": bool(observed_target(text, self.search)),
                "visible_chapter": observed_heading(text, self.search),
                "markers": [name for name, pattern in MARKERS.items() if pattern.search(text)],
            })
        return state

    def png_if_changed(self, previous_digest=None):
        image = super().png_if_changed(previous_digest)
        if self.witness_pixels and not any(page["visible_target"] for page in self.search.pages):
            started = time.monotonic()
            self.browser_state()
            self.search.pixel_verifier_ms += duration(started, time.monotonic())
        return image

    def close(self):
        try:
            super().close()
        finally:
            self.search.closed = time.monotonic()


class TracedProvider:
    is_cloud = False
    supports_images = True

    def __init__(self, backend, local, turn_limit, follow, click_link):
        self.backend, self.local = backend, local
        self.turn_limit, self.follow = turn_limit, follow
        self.click_type = "click_link" if click_link else "click_ref"

    def decide_with_usage(self, prompt, screenshot, cancel, *, directives):
        search = self.local.search
        if len(search.requests) >= self.turn_limit:
            raise RuntimeError("Benchmark decision limit reached")
        page_text = prompt.partition("\nVisible browser elements:\n")[2]
        refs = {ref for title, ref in targets(page_text, search) if title == search.title}
        clicked = any(turn.get("action") in BROWSER_INPUT for turn in search.requests)
        if self.follow:
            safe = {"done", "blocked", "wait", "visual", "continue"}
            if refs and not clicked:
                safe.add(self.click_type)
            directives = tuple(kind for kind in directives if kind in safe)
        row = {"started": time.monotonic(), "image_bytes": len(screenshot or b""),
               "prompt_bytes": len(prompt.encode()), "target_in_prompt": bool(refs)}
        if screenshot:
            row["image_sha256"] = hashlib.sha256(screenshot).hexdigest()
        search.requests.append(row)
        try:
            result, usage = self.backend.decide_with_usage(prompt, screenshot, cancel, directives=directives)
            row["action"] = result.get("type")
            if self.follow and result.get("type") in BROWSER_INPUT:
                target = result.get("url") if self.click_type == "click_link" else result.get("target")
                expected = {search.chapter} if self.click_type == "click_link" else refs
                row["chosen_target"] = target
                if not isinstance(target, str) or target not in expected:
                    row["action"] = "rejected_ref"
                    failure = ProviderResponseError("Use the reference printed on the line with the requested chapter's exact path.")
                    failure.usage = usage
                    raise failure
            return result, usage
        except ProviderResponseError as error:
            row["error"] = str(error)
            raise
        finally:
            row["finished"] = time.monotonic()


def duration(start, end):
    return round((end - start) * 1000, 2) if start and end else None


def overlap(intervals):
    intervals = [(start, end) for start, end in intervals if start and end and start < end]
    points = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals])
    counts = Counter()
    previous = points[0][0] if points else 0
    active = 0
    for at, change in points:
        counts[active] += at - previous
        active += change
        previous = at
    return {"peak": max((key for key, elapsed in counts.items() if elapsed > 0), default=0),
            "two_or_more_ms": round(sum(elapsed for key, elapsed in counts.items() if key >= 2) * 1000, 2),
            "three_ms": round(counts[3] * 1000, 2)}


def worker_report(search, began, snapshot, events, follow, title_only):
    job = search.job
    markers = sorted({marker for page in search.pages for marker in page["markers"]})
    click = next((row for row in search.requests if row.get("action") in BROWSER_INPUT), None)
    saw_target = any(page["visible_target"] and page["url"] == docs_url(search.search_url, search.search_url)
                     and (not click or page["at"] <= click["started"]) for page in search.pages)
    saw_chapter = bool(click and any(page["visible_chapter"] and page["url"] == search.chapter
                                    and page["at"] >= click["finished"] for page in search.pages))
    clicked = sum(event["kind"] == "action" and event["text"] == "Clicked a browser element." for event in events)
    answer_correct = answer_matches(snapshot["result"], search, title_only)
    correct = snapshot["status"] == "completed" and saw_target and answer_correct
    if follow:
        correct = correct and saw_chapter and clicked == 1
    if correct:
        finding = "verified_public_reference"
    elif saw_target:
        finding = "visible_reference_unverified_by_worker"
    elif markers:
        finding = markers[0]
    elif search.pages:
        finding = "no_matching_visible_reference"
    elif snapshot["status"] == "awaiting_input":
        finding = "model_reported_blocked_without_semantic_evidence"
    else:
        finding = "no_semantic_observation"
    return {"query": search.query, "id": job.id, "url": search.search_url,
            "status": snapshot["status"], "detail": snapshot["detail"], "finding": finding,
            "correct": correct, "answer_correct": answer_correct, "visible_target": saw_target,
            "verified_chapter_navigation": saw_chapter, "acknowledged_clicks": clicked,
            "verified_result": {"title": search.title, "url": search.chapter} if correct else None,
            "result": snapshot["result"], "browser_markers": markers, "semantic_observations": len(search.pages),
            "first_target_ms": duration(began, next((page["at"] for page in search.pages if page["visible_target"]), 0)),
            "pixel_verifier_ms": round(search.pixel_verifier_ms, 2),
            "desktop_start_ms": duration(began, search.started), "desktop_ready_ms": duration(began, search.ready),
            "desktop_closed_ms": duration(began, search.closed), "model_usage": dict(job.model_usage),
            "requests": [{"start_ms": duration(began, row["started"]),
                          "duration_ms": duration(row["started"], row.get("finished")),
                          **{key: value for key, value in row.items() if key not in {"started", "finished"}}}
                         for row in search.requests]}


def resources(gpu):
    meminfo = Path("/proc/meminfo").read_text()
    host_mem = int(re.search(r"^MemAvailable:\s+(\d+) kB$", meminfo, re.M)[1]) / 1024
    procs = {}
    for candidate in Path("/proc").iterdir():
        if candidate.name.isdecimal():
            try:
                parts = (candidate / "stat").read_text().rsplit(")", 1)[1].split()
                procs[int(candidate.name)] = (int(parts[1]), int(parts[21]))
            except (OSError, UnicodeError, ValueError, IndexError):
                continue
    selected = {os.getpid()}
    while (children := {pid for pid, (parent, _) in procs.items() if parent in selected} - selected):
        selected.update(children)
    rss = sum(procs[pid][1] for pid in selected & procs.keys()) * os.sysconf("SC_PAGE_SIZE") / 2**20
    sample = {"host_mem_available_mib": round(host_mem, 1), "benchmark_tree_rss_mib": round(rss, 1)}
    if gpu is not None:
        try:
            output = subprocess.check_output(["nvidia-smi", f"--id={gpu}",
                "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                timeout=3, text=True, stderr=subprocess.DEVNULL)
            sample["gpu_used_mib"], sample["gpu_utilization_pct"] = map(int, output.strip().split(","))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return sample


def benchmark(model, searches, args):
    by_url = {search.search_url: search for search in searches}
    local = threading.local()
    provider = TracedProvider(model, local, args.turn_limit, args.follow, args.click_link)
    gpu = args.gpu if args.gpu_layers else None

    def factory(template, workspace, **options):
        search = local.search = by_url[options["start_url"]]
        return BenchDesktop(template, search, args.mode == "pixels", workspace=workspace, **options)

    with tempfile.TemporaryDirectory(prefix="pcu-live-docs-") as state:
        app = Orchestrator(Path(state), factory, {"local": provider},
                           sessions=len(searches), inference=len(searches), network_domains=(HOST,),
                           validate_browser_url=validate_network_target, observation_mode=args.mode)
        try:
            initial = resources(gpu)
            samples = [initial]
            sampling = threading.Event()

            def sample_usage():
                while not sampling.wait(0.5):
                    samples.append(resources(gpu))

            monitor = threading.Thread(target=sample_usage, daemon=True)
            monitor.start()
            began = time.monotonic()
            try:
                run_id = app.start_run({"prompt": "Find Python reference chapters through public documentation search.",
                                        "assignments": [assignment(search, args.follow, args.answer_title, args.click_link)
                                                        for search in searches]})
                with app.cv:
                    jobs = app.runs[run_id].jobs
                    for search, job in zip(searches, jobs, strict=True):
                        search.job = job
                    finished = app.cv.wait_for(lambda: all(job.status in TERMINAL | {"awaiting_input"} for job in jobs),
                                               args.timeout)
                    at = time.monotonic()
                    snapshot = next(run for run in app.state()[1]["runs"] if run["id"] == run_id)
                if any(job.status not in TERMINAL for job in jobs):
                    app.cancel_run(run_id)
                    with app.cv:
                        app.cv.wait_for(lambda: all(job.status in TERMINAL for job in jobs), timeout=12)
            finally:
                sampling.set()
                monitor.join(timeout=4)
            requests = list(chain.from_iterable(search.requests for search in searches))
            rows = [worker_report(search, began, item, [event for event in snapshot["events"]
                    if event["job_id"] == search.job.id], args.follow, args.answer_title)
                    for search, item in zip(searches, snapshot["jobs"], strict=True)]
            aggregate = Counter()
            for row in rows:
                aggregate.update(row["model_usage"])
            resource_summary = {"samples": len(samples), "start": initial,
                                "peak_rss_mib": max(item["benchmark_tree_rss_mib"] for item in samples),
                                "min_host_mem_available_mib": min(item["host_mem_available_mib"] for item in samples)}
            for metric in ("gpu_used_mib", "gpu_utilization_pct"):
                if readings := [item[metric] for item in samples if metric in item]:
                    resource_summary[f"peak_{metric}"] = max(readings)
            return {"run_id": run_id, "status": snapshot["status"], "wall_ms": duration(began, at),
                    "timed_out": not finished, "verified_workers": sum(row["correct"] for row in rows),
                    "workers": rows, "model_usage": dict(aggregate), "resources": resource_summary,
                    "desktop_overlap": overlap((search.ready, min(search.closed or at, at)) for search in searches),
                    "request_overlap": overlap((row["started"], min(row.get("finished", at), at)) for row in requests)}
        finally:
            app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("server", "gguf", "projector"):
        parser.add_argument(f"--{field}", type=lambda name: Path(name).expanduser().resolve(strict=True), required=True)
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--gpu-layers", type=int, default=99)
    parser.add_argument("--context", type=int, default=6144)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=75)
    parser.add_argument("--turn-limit", type=int, default=8)
    parser.add_argument("--output-tokens", type=int, default=384)
    parser.add_argument("--follow", "--action", action="store_true", help="Follow one reference link per worker and verify its destination")
    parser.add_argument("--click-link", action="store_true", help="Match the exact visible link URL for navigation")
    parser.add_argument("--mode", choices=("targeted", "pixels"), default="targeted")
    parser.add_argument("--answer-title", action="store_true", help="Require only the title from the model for a fair pixel comparison")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    if min(args.timeout, args.turn_limit, args.context, args.threads, args.output_tokens) <= 0 or args.gpu_layers < 0:
        parser.error("Timeout, turn limit, context and threads must be positive, and GPU layers cannot be negative")
    if args.mode == "pixels" and (not args.answer_title or args.follow):
        parser.error("Pixel mode requires --answer-title because a read-only screenshot does not expose link URLs")
    if args.follow and args.answer_title:
        parser.error("The reference navigation requires a title and URL")
    if args.click_link and not args.follow:
        parser.error("--click-link requires --follow")
    searches = [Search(query) for query in QUERIES]
    allowed_searches(searches, follow=args.follow)
    baseline = resources(args.gpu if args.gpu_layers else None)
    managed = ManagedModel(str(args.server), args.gguf, args.projector, slots=len(searches),
                           context_per_slot=args.context, gpu_layers=args.gpu_layers, threads=args.threads,
                           gpu_device=args.gpu if args.gpu_layers else None)
    if args.no_cache:
        managed.options.append("--no-cache-prompt")
    began = time.monotonic()
    with managed as server:
        start_ms = duration(began, time.monotonic())
        model = LlamaProvider(server.alias, server.base_url, api_key=server.api_key, reasoning_budget=96,
                              max_output_tokens=args.output_tokens, timeout=min(args.timeout, 30))
        report = benchmark(model, searches, args)
    report["configuration"] = {"viewport": VIEWPORT, "mode": args.mode, "slots": len(searches),
                               "context_per_slot": args.context, "gguf": args.gguf.name, "projector": args.projector.name,
                               "model_start_ms": start_ms, "prompt_cache": not args.no_cache, "gpu_layers": args.gpu_layers,
                               "max_output_tokens": args.output_tokens,
                               "click_type": ("click_link" if args.click_link else "click_ref") if args.follow else None,
                               "task": "follow_reference" if args.follow else "read_only_search",
                               "answer": "title" if args.answer_title else "title_and_url",
                               "pre_model_resources": baseline, "post_model_resources": resources(args.gpu if args.gpu_layers else None)}
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return int(report["verified_workers"] != len(searches))


if __name__ == "__main__":
    raise SystemExit(main())
