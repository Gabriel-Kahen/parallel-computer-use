"""Launch the desktop console or check runtime prerequisites."""

import argparse
import fcntl
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import webbrowser
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .codex_auth import CodexAuth, CodexAuthError
from .control import ControlSocket, send
from .model_process import ManagedModel
from .orchestrator import BROWSER_OBSERVATION_MODES, OBSERVATION_MODES, Orchestrator, TEMPLATES
from .providers import CodexProvider, GeminiProvider, LlamaProvider, ProviderError
from .server import LocalServer
from .store import Store


class DemoProvider:
    is_cloud = False
    supports_images = True

    def decide(self, *_args):
        return {"type": "done", "result": "Scripted check finished."}


class DesktopNotifications:
    def __init__(self, enabled):
        self.executable = shutil.which("notify-send") if enabled else None
        self.messages = queue.Queue(maxsize=12)
        self.stop = threading.Event()
        self.thread = None
        if self.executable:
            self.thread = threading.Thread(target=self._run, name="pcu-notifications", daemon=True)
            self.thread.start()

    def offer(self, title, text):
        if self.thread:
            try:
                self.messages.put_nowait((title, text))
            except queue.Full:
                pass

    def _run(self):
        while not self.stop.is_set():
            try:
                title, text = self.messages.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                subprocess.run([self.executable, "--app-name=Parallel Computer Use", "--", title, text],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2.5)


def state_directory():
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    current = base / "parallel-computer-use"
    previous = base / "local-computer-use"
    return previous if previous.exists() and not current.exists() else current


def app_environment(name, default=None):
    return os.environ.get(f"PCU_{name}", os.environ.get(f"LCU_{name}", default))


def secure_directory(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The state directory must be user-owned, private (0700), and not a symlink.")


def lock_directory(directory):
    descriptor = os.open(directory / "service.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise ValueError("A service is already using this state directory.") from None
    return os.fdopen(descriptor, "wb")


def doctor(_args):
    bins = ("Xvfb", "bwrap", "i3", "dbus-run-session", "xterm", "nano", "xclip",
            "firefox", "chromium", "llama-server")
    data = {
        "python": sys.version.split()[0],
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "executables": {name: bool(shutil.which(name)) for name in bins},
        "llama_url_configured": bool(app_environment("LLAMA_URL")),
        "gemini_configured": bool(app_environment("GEMINI_API_KEY") and app_environment("GEMINI_MODEL")),
        "codex_configured": bool(app_environment("CODEX_TOKEN") and app_environment("CODEX_MODEL")),
    }
    data["executables"]["chrome"] = os.access("/opt/google/chrome/chrome", os.X_OK)
    data["available_disk_mib"] = shutil.disk_usage(Path.home()).free // (1024 * 1024)
    print(json.dumps(data, indent=2))
    return 0 if all(data["executables"][name] for name in bins[:7]) else 1


def configured_providers(args, managed=None, config=None):
    if args.demo:
        return {"local": DemoProvider()}
    identifier, url = (managed.alias, managed.base_url) if managed else (args.model, args.llama_url)
    api_key = managed.api_key if managed else app_environment("LLAMA_API_KEY")
    providers = {"local": LlamaProvider(identifier, url, timeout=args.model_timeout, api_key=api_key,
                                       max_output_tokens=args.model_max_tokens,
                                       reasoning_budget=args.model_reasoning_budget)}
    for name, entry in (config or {}).get("local_models", {}).items():
        provider = LlamaProvider(entry["model"], entry["url"], timeout=args.model_timeout,
                                 max_output_tokens=entry.get("max_output_tokens"),
                                 reasoning_budget=entry.get("reasoning_budget"))
        provider.label = entry.get("label", name)
        providers["local-" + name] = provider
    if key := app_environment("GEMINI_API_KEY"):
        if model := app_environment("GEMINI_MODEL"):
            providers["gemini"] = GeminiProvider(model, key, timeout=args.model_timeout)
    if args.codex_model:
        if app_environment("CODEX_TOKEN"):
            supplier = lambda: app_environment("CODEX_TOKEN", "")
        else:
            auth = CodexAuth(args.state_dir.absolute() / "codex-auth")
            signed_in = [account.client_id for account in auth.accounts() if account.signed_in]
            selected = args.codex_account or (signed_in[0] if len(signed_in) == 1 else None)
            if selected not in signed_in:
                raise ValueError("Select a signed-in Codex account. Run codex-accounts to list accounts.")
            supplier = lambda: auth.access_token(selected)
        providers["codex"] = CodexProvider(args.codex_model, supplier, timeout=args.model_timeout)
    return providers


def codex_account(args):
    directory = args.state_dir.absolute()
    secure_directory(directory)
    auth = CodexAuth(directory / "codex-auth")
    if args.command == "codex-login":
        accounts = [auth.sign_in(args.account)]
    elif args.command == "codex-logout":
        account = next((item for item in auth.accounts() if item.client_id == args.account), None)
        if account is None:
            raise ValueError("No Codex account has that ID.")
        revoked = auth.sign_out(account.client_id)
        print(json.dumps({"signed_in": False, "revocation_confirmed": revoked}))
        return 0
    else:
        accounts = auth.accounts()
    print(json.dumps([{"account": a.client_id, "email": a.email, "signed_in": a.signed_in}
                      for a in accounts], indent=2))
    return 0


def client(args):
    path = args.state_dir.absolute() / "control.sock"
    try:
        reply = send(path, args.command)
    except FileNotFoundError as error:
        raise ValueError("No running service was found in this state directory.") from error
    if args.command == "open":
        if args.print_url or not webbrowser.open(reply["url"]):
            print(reply["url"])
    else:
        print(json.dumps(reply))
    return 0


def prune_history(args):
    directory = args.state_dir.absolute()
    if not (directory / "runs.sqlite3").is_file():
        print(json.dumps({"removed": 0}))
        return 0
    secure_directory(directory)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.older_than_days)).isoformat()
    counted = 0
    with lock_directory(directory):
        store = Store(directory)
        try:
            for snapshot in store.older_runs(cutoff):
                counted += 1
                if args.dry_run:
                    continue
                for child in snapshot["jobs"]:
                    job_id = child["id"]
                    if re.fullmatch(r"[0-9a-f]{32}", job_id):
                        workspace = directory / "work" / job_id
                        if workspace.is_dir() and not workspace.is_symlink():
                            shutil.rmtree(workspace)
                run_id = snapshot["id"]
                if re.fullmatch(r"[0-9a-f]{32}", run_id):
                    inputs = directory / "imports" / run_id
                    if inputs.is_dir() and not inputs.is_symlink():
                        shutil.rmtree(inputs)
                store.remove(run_id)
        finally:
            store.close()
    print(json.dumps({"matched" if args.dry_run else "removed": counted}))
    return 0


def read_config(path):
    if path is None:
        return {}
    with path.open("rb") as stream:
        body = stream.read(64 * 1024 + 1)
    if len(body) > 64 * 1024:
        raise ValueError("Application configuration may contain at most 64 KiB.")
    config = json.loads(body)
    if not isinstance(config, dict) or set(config) - {"applications", "local_models"}:
        raise ValueError("Configuration supports 'applications' and 'local_models' objects.")
    for group in ("applications", "local_models"):
        entries = config.get(group, {})
        if not isinstance(entries, dict) or len(entries) > 32:
            raise ValueError(f"Configure up to 32 entries in {group}.")
        for name, entry in entries.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) or not isinstance(entry, dict):
                raise ValueError(f"{group} entries need lowercase names and settings objects.")
            label = entry.get("label", name)
            if not isinstance(label, str) or not 1 <= len(label) <= 60 or not label.isprintable():
                raise ValueError(f"{group} labels must be 1–60 printable characters.")
            required = {"argv"} if group == "applications" else {"url", "model"}
            optional = {"label"} if group == "applications" else {"label", "max_output_tokens", "reasoning_budget"}
            if not required <= set(entry) or set(entry) - (required | optional):
                raise ValueError(f"Incomplete settings for {name} in {group}.")
            if group == "local_models" and not all(isinstance(entry[key], str) for key in required):
                raise ValueError(f"Local model {name} requires a URL and model identifier.")
    return config


def serve(args):
    from .desktop import DesktopSession

    network_domains = ()
    network_validator = None
    config = read_config(args.config)
    native = {}
    templates = dict(TEMPLATES)
    apps = config.get("applications", {})
    if args.native_executable or apps:
        from .desktop import validate_native_argv

        if args.desktop_mode != "sandbox":
            raise ValueError("Custom applications require sandbox mode.")
        if args.native_executable:
            native["native"] = validate_native_argv((args.native_executable, *args.native_arg))
            templates["native"] = args.native_label or Path(native["native"][0]).name
        for name, entry in apps.items():
            key = "app-" + name
            native[key] = validate_native_argv(entry["argv"])
            templates[key] = entry.get("label", name)
    elif args.native_arg or args.native_label:
        raise ValueError("Specify --native-executable to configure a custom application.")
    if args.allow_domain:
        from ._network import normalize_domains, validate_network_target

        if args.desktop_mode != "sandbox":
            raise ValueError("Browser network access requires sandbox mode.")
        network_domains = tuple(sorted(normalize_domains(args.allow_domain)))
        network_validator = validate_network_target
    directory = args.state_dir.absolute()
    secure_directory(directory)
    profiles = tuple(sorted(set(args.browser_profile)))
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name) for name in profiles):
        raise ValueError("Browser profiles must be short names using letters, numbers, hyphens or underscores.")
    if profiles and args.desktop_mode != "sandbox":
        raise ValueError("Reusable browser profiles require sandbox mode.")
    if args.mmproj and not args.model_file:
        raise ValueError("--mmproj requires a managed --model-file.")
    if args.model_file and args.demo:
        raise ValueError("Select a managed model or scripted mode.")
    with ExitStack() as resources:
        resources.enter_context(lock_directory(directory))
        profile_root = directory / "browser-profiles"
        if profiles:
            secure_directory(profile_root)
            for name in profiles:
                secure_directory(profile_root / name)
        managed = None
        if args.model_file:
            managed = resources.enter_context(ManagedModel(
                args.llama_server, args.model_file, args.mmproj,
                slots=args.inference_slots, context_per_slot=args.context_per_slot,
                gpu_layers=args.gpu_layers, threads=args.model_threads, gpu_device=args.gpu_device,
                image_min_tokens=args.image_min_tokens,
            ))
        providers = configured_providers(args, managed, config)
        notifications = DesktopNotifications(args.notifications)
        resources.callback(notifications.close)

        def create_desktop(template, workspace, *, start_url=None, browser_profile=None):
            options = {"allowed_domains": network_domains, "start_url": start_url} if start_url else {}
            if template in native:
                options["native_argv"] = native[template]
                template = "native"
            if browser_profile:
                options["browser_profile"] = profile_root / browser_profile
            return DesktopSession(template, mode=args.desktop_mode, workspace=workspace,
                                  width=args.width, height=args.height,
                                  browser_observer=args.observation_mode in BROWSER_OBSERVATION_MODES, **options)

        app = Orchestrator(directory, create_desktop, providers, demo=args.demo,
                           sessions=args.sessions, inference=args.inference_slots,
                           max_workers=args.max_workers, network_domains=network_domains,
                           validate_browser_url=network_validator, notify=notifications.offer,
                           templates=templates, browser_profiles=profiles,
                           observation_mode=args.observation_mode, model_pointer_units=args.model_pointer_units,
                           memory_floor=args.memory_reserve_mib * 1024 * 1024)
        try:
            with LocalServer(app, args.port) as server:
                with ControlSocket(directory / "control.sock", server):
                    launch = server.new_launch_url()
                    print(f"Open the local app: {launch}", flush=True)

                    def shutdown(_signum, _frame):
                        threading.Thread(target=server.shutdown, daemon=True).start()

                    previous = signal.signal(signal.SIGTERM, shutdown)
                    try:
                        if args.open:
                            webbrowser.open(launch)
                        server.serve_forever(poll_interval=0.25)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
        except KeyboardInterrupt:
            pass
        finally:
            app.close()
    return 0


def bounded(low, high):
    def parse(value):
        try:
            value = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("Expected an integer.") from error
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"Expected a value from {low} to {high}.")
        return value
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(prog="parallel-computer-use")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Report which local components are configured.").set_defaults(run=doctor)
    history = commands.add_parser("prune-history", help="Remove old finished runs while the service is stopped.")
    history.add_argument("--state-dir", type=Path, default=state_directory())
    history.add_argument("--older-than-days", type=bounded(1, 36500), required=True)
    history.add_argument("--dry-run", action="store_true", help="Count eligible runs without deleting anything.")
    history.set_defaults(run=prune_history)
    for name, helper in (("open", "Open a new local dashboard link."),
                         ("status", "Report running worker counts."),
                         ("stop-all", "Cancel all active and queued workers.")):
        command = commands.add_parser(name, help=helper)
        command.add_argument("--state-dir", type=Path, default=state_directory())
        if name == "open":
            command.add_argument("--print-url", action="store_true", help="Print the one-use dashboard link.")
        command.set_defaults(run=client)
    for name, helper in (("codex-login", "Authorize ChatGPT plan usage through the official login."),
                         ("codex-accounts", "List the locally connected Codex accounts."),
                         ("codex-logout", "Disconnect a locally connected Codex account.")):
        command = commands.add_parser(name, help=helper)
        command.add_argument("--state-dir", type=Path, default=state_directory())
        if name != "codex-accounts":
            command.add_argument("--account", required=name == "codex-logout")
        command.set_defaults(run=codex_account)
    server = commands.add_parser("serve", help="Start the local dashboard and worker service.")
    server.add_argument("--state-dir", type=Path, default=state_directory())
    server.add_argument("--config", type=Path, help="JSON with additional trusted apps and local model endpoints.")
    server.add_argument("--port", type=bounded(0, 65535), default=0)
    server.add_argument("--sessions", type=bounded(1, 12), default=3)
    server.add_argument("--inference-slots", type=bounded(1, 12), default=1)
    server.add_argument("--max-workers", type=bounded(1, 100), default=32)
    server.add_argument("--memory-reserve-mib", type=bounded(256, 65536), default=1024)
    server.add_argument("--model-timeout", type=bounded(5, 900), default=90)
    server.add_argument("--model-max-tokens", type=bounded(32, 32768),
                        help="Optional total output-token cap per local model turn.")
    server.add_argument("--model-reasoning-budget", type=bounded(0, 32768),
                        help="Optional llama-server reasoning budget for the primary local model.")
    server.add_argument("--width", type=bounded(640, 1280), default=960)
    server.add_argument("--height", type=bounded(480, 960), default=640)
    server.add_argument("--llama-url", default=app_environment("LLAMA_URL", "http://127.0.0.1:8080/v1"))
    server.add_argument("--model", default=app_environment("LLAMA_MODEL", "local"))
    server.add_argument("--model-file", type=Path, help="Manage one local GGUF model instead of connecting to a server.")
    server.add_argument("--mmproj", type=Path, help="Matching vision projector for a managed model.")
    server.add_argument("--llama-server", default=app_environment("LLAMA_SERVER", "llama-server"))
    server.add_argument("--context-per-slot", type=bounded(2048, 32768), default=4096)
    server.add_argument("--gpu-layers", type=bounded(0, 99), default=99)
    server.add_argument("--gpu-device", type=bounded(0, 31), help="Select a CUDA device for the managed model.")
    server.add_argument("--model-threads", type=bounded(1, 12), default=2)
    server.add_argument("--image-min-tokens", type=bounded(16, 4096),
                        help="Optional minimum visual tokens per frame for the managed model.")
    server.add_argument("--codex-model", default=app_environment("CODEX_MODEL"))
    server.add_argument("--codex-account", default=app_environment("CODEX_ACCOUNT"))
    server.add_argument("--desktop-mode", choices=("sandbox", "host-private"), default="sandbox")
    server.add_argument("--observation-mode", choices=OBSERVATION_MODES, default="pixels",
                        help="Opt-in local observer experiment. Browser observers cover the offline Chromium catalog.")
    server.add_argument("--model-pointer-units", choices=("pixels", "normalized"),
                        help="Fix the coordinate convention for local model pointer actions.")
    server.add_argument("--allow-domain", action="append", default=[],
                        help="Permit an exact hostname in opt-in browser jobs. May be repeated.")
    server.add_argument("--native-executable", help="Add one trusted X11 application from /usr/bin.")
    server.add_argument("--native-arg", action="append", default=[], help="Fixed native app argument. May be repeated.")
    server.add_argument("--native-label", help="Name for the custom application in the dashboard.")
    server.add_argument("--browser-profile", action="append", default=[],
                        help="Offer a named, reusable private browser profile. May be repeated.")
    server.add_argument("--notifications", action="store_true", help="Show passive desktop notifications.")
    server.add_argument("--demo", action="store_true", help="Use scripted workers instead of a model.")
    server.add_argument("--open", action="store_true", help="Open the dashboard in your usual browser.")
    server.set_defaults(run=serve)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (OSError, ValueError, ProviderError, CodexAuthError) as error:
        print(f"Parallel Computer Use: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
