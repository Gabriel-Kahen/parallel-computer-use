"""Measure a temporary local llama-server with synthetic images and a private editor."""

import argparse
import binascii
import http.client
import json
import os
import socket
import statistics
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from parallel_computer_use import providers
from parallel_computer_use.desktop import DesktopSession
from parallel_computer_use.orchestrator import Orchestrator, TERMINAL


COLORS = {"red": b"\xff\x00\x00", "green": b"\x00\xff\x00", "blue": b"\x00\x00\xff"}
SENTENCE = "Local VLM fixture verified."


def image(color, variant):
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", binascii.crc32(kind + data))

    size = 96 + variant * 4
    rows = (b"\x00" + COLORS[color] * size) * size
    header = struct.pack("!IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def gpu_sample(index):
    line = subprocess.check_output([
        "nvidia-smi", f"--id={index}", "--query-gpu=memory.total,memory.used,utilization.gpu",
        "--format=csv,noheader,nounits",
    ], timeout=3, text=True)
    return [int(item.strip()) for item in line.strip().split(",")]


@contextmanager
def observe_gpu(index):
    for _ in range(12):
        baseline = gpu_sample(index)
        total, used, utilization = baseline
        if total - used >= 10240 and utilization <= 10:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("The GPU has insufficient idle headroom for this benchmark")
    samples = [baseline]
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.5):
            try:
                samples.append(gpu_sample(index))
            except (subprocess.SubprocessError, ValueError, OSError):
                continue

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        yield samples
    finally:
        stop.set()
        thread.join()


def ready(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/health")
        with connection.getresponse() as response:
            return response.status == 200 and json.loads(response.read(1024)).get("status") == "ok"
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        connection.close()


@contextmanager
def llama_server(args, slots):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        began = time.monotonic()
        process = subprocess.Popen([
            str(args.server), "--model", str(args.gguf), "--mmproj", str(args.projector),
            "--host", "127.0.0.1", "--port", str(port), "--alias", "pcu-benchmark",
            "--ctx-size", str(args.context), "--parallel", str(slots), "--n-gpu-layers", "all",
            "--flash-attn", "off", "--no-webui",
        ], stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "CUDA_VISIBLE_DEVICES": str(args.gpu)})
        try:
            while process.poll() is None and time.monotonic() - began < 35:
                if ready(port):
                    yield providers.LlamaProvider("pcu-benchmark", f"http://127.0.0.1:{port}/v1", timeout=15), (
                        time.monotonic() - began
                    )
                    break
                time.sleep(0.12)
            else:
                log.seek(max(0, os.fstat(log.fileno()).st_size - 5000))
                raise RuntimeError(f"llama-server did not become healthy:\n{log.read().decode(errors='replace')}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def request_batch(provider, slots, wave):
    barrier = threading.Barrier(slots, timeout=3)

    def request(index):
        expected = tuple(COLORS)[(index + wave) % len(COLORS)]
        screenshot = image(expected, wave)
        prompt = (
            f"Fixture {wave}-{index}. The whole picture is a solid color. "
            "Identify the center pixel as red, green, or blue. "
            'Return exactly {"type":"done","result":"one lowercase color"}.'
        )
        barrier.wait()
        start = time.monotonic()
        try:
            result = provider.decide(prompt, screenshot, threading.Event())
            correct = result == {"type": "done", "result": expected}
        except providers.ProviderError as error:
            result, correct = str(error), False
        return {"expected": expected, "response": result, "correct": correct, "seconds": time.monotonic() - start}

    with ThreadPoolExecutor(max_workers=slots) as pool:
        began = time.monotonic()
        individual = list(pool.map(request, range(slots)))
        elapsed = time.monotonic() - began
    return individual, elapsed


class BoundedProvider:
    is_cloud = False
    supports_images = True

    def __init__(self, backend):
        self.backend = backend
        self.actions = []

    def decide(self, prompt, screenshot, cancel):
        if len(self.actions) == 8:
            raise RuntimeError("The qualification reached its eight-decision limit")
        began = time.monotonic()
        result = self.backend.decide(prompt, screenshot, cancel)
        self.actions.append({"directive": result, "seconds": time.monotonic() - began})
        return result


def desktop_tasks(provider):
    viewport = {"width": 640, "height": 480}
    with DesktopSession("editor", **viewport) as desktop:
        screenshot = desktop.png()
        began = time.monotonic()
        probe = provider.decide(
            'Read the real editor screenshot. Return {"type":"done","result":"<application> | <full file path>"}.',
            screenshot, threading.Event(),
        )
        read = {"directive": probe, "seconds": time.monotonic() - began, "bytes": len(screenshot)}
    tracked = BoundedProvider(provider)
    type_directive = json.dumps({"type": "type", "text": SENTENCE})
    prompt = (
        f'Type exactly "{SENTENCE}" in the empty notes file in GNU nano and save it. '
        "The file is open and writable. Use Recent actions to pick the next step. "
        f'If Recent actions has no "Entered text.", use {type_directive}. '
        'If the only prior action was "Entered text.", use {"type":"key","key":"ctrl+s"}. '
        'Ctrl+S saves the existing file directly. Once "Pressed a key." is recorded or the footer says '
        '"Wrote 1 line", report done. Do not use the Write Out dialog.'
    )
    with tempfile.TemporaryDirectory(prefix="pcu-vlm-edit-") as state:
        app = Orchestrator(
            Path(state), lambda template, workspace: DesktopSession(template, workspace=workspace, **viewport),
            {"local": tracked}, sessions=1, inference=1, memory_floor=0,
        )
        began = time.monotonic()
        try:
            run = app.start_run({"prompt": prompt})
            job = app.runs[run].jobs[0]
            with app.cv:
                app.cv.wait_for(lambda: job.status in TERMINAL or job.status == "awaiting_input", timeout=50)
                status, detail = job.status, job.detail
            artifact = app.artifact(job.id, "notes.txt") if status == "completed" and "notes.txt" in job.artifacts else b""
            edit = {
                "status": status, "detail": detail, "decisions": tracked.actions,
                "artifact": artifact.decode(errors="replace"), "correct": artifact == (SENTENCE + "\n").encode(),
                "seconds": time.monotonic() - began,
            }
        finally:
            app.close()
    return {"screen_read": read, "edit": edit}


def main():
    parser = argparse.ArgumentParser(usage="python -m scripts.bench_local [options]")
    for option in ("server", "gguf", "projector"):
        parser.add_argument(f"--{option}", type=lambda name: Path(name).expanduser().resolve(strict=True), required=True)
    parser.add_argument("--slots", type=int, nargs="+", choices=(1, 2, 3), default=[1, 2, 3])
    parser.add_argument("--waves", type=int, choices=range(1, 7), default=3)
    parser.add_argument("--context", type=int, choices=(4096, 6144, 8192), default=6144)
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--skip-desktop", action="store_true")
    args = parser.parse_args()
    with patch.object(providers, "MAX_OUTPUT_TOKENS", 128):
        for slots in dict.fromkeys(args.slots):
            with observe_gpu(args.gpu) as samples, llama_server(args, slots) as (provider, startup):
                warmups = []
                for wave in (10, 11):
                    warmup, _ = request_batch(provider, slots, wave)
                    warmups.extend(warmup)
                    if all(item["correct"] for item in warmup):
                        break
                requests, elapsed = [], 0
                for wave in range(1, args.waves + 1):
                    items, duration = request_batch(provider, slots, wave)
                    requests.extend(items)
                    elapsed += duration
                report = {
                    "slots": slots, "total_context": args.context, "per_slot_context": args.context // slots,
                    "startup_seconds": startup, "warmup": warmups,
                    "correct": sum(item["correct"] for item in requests),
                    "measurement_requests": len(requests), "measurement_wall_seconds": elapsed,
                    "requests_per_second": len(requests) / elapsed,
                    "median_latency_seconds": statistics.median(item["seconds"] for item in requests),
                    "slowest_latency_seconds": max(item["seconds"] for item in requests),
                    "baseline_vram_mib": samples[0][1], "peak_vram_mib": max(row[1] for row in samples),
                    "gpu_samples": len(samples), "requests": requests,
                }
                if slots == 1 and not args.skip_desktop:
                    report.update(desktop_tasks(provider))
                    report["desktop_peak_vram_mib"] = max(row[1] for row in samples)
                print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
