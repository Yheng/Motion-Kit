#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
motion-kit — build scroll-driven cinematic landing pages.

The whole CLI lives in this file. Python 3.9+, standard library only, no pip
installs, no build step. Cross-platform: Windows, macOS, Linux.

The effect this tool builds is a canvas image-sequence scrub: a short clip is
exported to numbered frames, preloaded, and the frame painted to a <canvas> is
chosen by scroll position. See BUILD-SPEC.md for the full design.

Usage:
    python motionkit.py doctor
    python motionkit.py init <project> [--provider fal|gemini|byo]
    python motionkit.py cost --project <project>
    python motionkit.py serve --project <project> [--port 8000]
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.1.0"

# ── paths ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "out"
KIT_DIR = ROOT / "kit"
PROVIDERS_DIR = ROOT / "providers"
FFMPEG_DIR = ROOT / ".ffmpeg"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""

#: state.json phase vocabulary — the eight consultation phases plus a terminal one.
PHASES = [
    "intake", "positioning", "directions", "architecture",
    "copy", "still", "motion", "assembly", "done",
]

DEFAULTS = {
    "count": 180,
    "width": 1600,
    "mobile_width": 900,
    "format": "webp",
    "quality": 80,
}


# ── console ──────────────────────────────────────────────────────────────────

_UNICODE = True


def init_console() -> None:
    """Force UTF-8 on stdout/stderr, then work out what the stream can render.

    On Windows ``sys.stdout.encoding`` is the ANSI codepage (cp1252 on a stock
    install) whenever stdout is a pipe rather than a console — which is how this
    CLI gets run by an agent, by a CI job, and by ``… > log.txt``. Printing the
    box-drawing and arrow characters used below then raises UnicodeEncodeError
    and kills the process. Reconfiguring to UTF-8 fixes it; the probe afterwards
    covers the case where reconfigure is unavailable or refused.
    """
    global _UNICODE
    for stream in (sys.stdout, sys.stderr):
        try:
            # line_buffering keeps stdout and stderr in the order they were
            # written; piped stdout is otherwise block-buffered and the two
            # streams interleave wrongly in any captured log.
            stream.reconfigure(  # type: ignore[attr-defined]
                encoding="utf-8", errors="replace", line_buffering=True,
            )
        except (AttributeError, ValueError, OSError):
            pass
    try:
        "─→✓✗≈".encode(sys.stdout.encoding or "ascii")
        _UNICODE = True
    except (UnicodeEncodeError, LookupError):
        _UNICODE = False


def mark(kind: str) -> str:
    """A status glyph that degrades to ASCII when the console cannot encode it."""
    if _UNICODE:
        return {"ok": "✓", "bad": "✗", "warn": "!", "dot": "·"}[kind]
    return {"ok": "OK", "bad": "X", "warn": "!", "dot": "-"}[kind]


def say(msg: str = "") -> None:
    print(msg)


def warn(msg: str) -> None:
    print(f"{mark('warn')} {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type] # noqa: F821
    print(f"{mark('bad')} {msg}", file=sys.stderr)
    sys.exit(code)


def rule(title: str = "") -> None:
    bar = "─" if _UNICODE else "-"
    if title:
        say(f"\n{title} {bar * max(4, 66 - len(title))}")
    else:
        say(bar * 68)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── .env ─────────────────────────────────────────────────────────────────────

def load_env() -> None:
    """Load .env into os.environ without overwriting real environment variables.

    Copies .env.example -> .env on first run, so a new checkout has a file to
    fill in rather than a missing one to guess at.
    """
    if not ENV_FILE.exists() and ENV_EXAMPLE.exists():
        try:
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
            say(f"{mark('dot')} created .env from .env.example — add a key, then re-run")
        except OSError as exc:
            warn(f"could not create .env: {exc}")

    if not ENV_FILE.exists():
        return

    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        warn(f"could not read .env: {exc}")
        return

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # A real environment variable always wins over the file.
        if key and key not in os.environ:
            os.environ[key] = value


# ── ffmpeg ───────────────────────────────────────────────────────────────────

def _tool_works(path: Path) -> bool:
    try:
        proc = subprocess.run(
            [str(path), "-version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_tool(name: str) -> "Path | None":
    """Locate ffmpeg/ffprobe: PATH first, then the bootstrapped .ffmpeg/ copy."""
    found = shutil.which(name)
    if found and _tool_works(Path(found)):
        return Path(found)
    local = FFMPEG_DIR / (name + EXE)
    if local.exists() and _tool_works(local):
        return local
    return None


def tool_version(path: Path) -> str:
    try:
        proc = subprocess.run(
            [str(path), "-version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20,
        )
        first = proc.stdout.decode("utf-8", "replace").splitlines()[0]
        return first.strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown version"


def _ffmpeg_archives() -> "list[str]":
    """Static-build URLs for this platform. Two entries on macOS (separate ffprobe)."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows":
        return ["https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"]
    if system == "Darwin":
        return [
            "https://evermeet.cx/ffmpeg/getrelease/zip",
            "https://evermeet.cx/ffprobe/getrelease/zip",
        ]
    if system == "Linux":
        arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
        return [f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz"]
    return []


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": f"motion-kit/{__version__}"})
    with urllib.request.urlopen(request, timeout=180) as response, open(dest, "wb") as handle:
        shutil.copyfileobj(response, handle)


def _extract_binaries(archive: Path, wanted: "set[str]", dest: Path) -> "list[str]":
    """Pull ffmpeg/ffprobe out by basename, ignoring however the archive is laid out."""
    extracted: "list[str]" = []
    dest.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                base = os.path.basename(member)
                if base in wanted:
                    with bundle.open(member) as src, open(dest / base, "wb") as out:
                        shutil.copyfileobj(src, out)
                    extracted.append(base)
        return extracted

    try:
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                base = os.path.basename(member.name)
                if member.isfile() and base in wanted:
                    src = bundle.extractfile(member)
                    if src is None:
                        continue
                    with src, open(dest / base, "wb") as out:
                        shutil.copyfileobj(src, out)
                    extracted.append(base)
    except tarfile.TarError as exc:
        warn(f"could not read archive {archive.name}: {exc}")
    return extracted


def bootstrap_ffmpeg() -> bool:
    """Download a static ffmpeg/ffprobe into .ffmpeg/. No package manager, no sudo."""
    urls = _ffmpeg_archives()
    if not urls:
        warn(f"no static ffmpeg build known for {platform.system()} — install ffmpeg manually")
        return False

    wanted = {"ffmpeg" + EXE, "ffprobe" + EXE}
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    say(f"{mark('dot')} ffmpeg not found — downloading a static build into .ffmpeg/")

    got: "set[str]" = set()
    with tempfile.TemporaryDirectory() as tmp:
        for url in urls:
            suffix = ".zip" if url.endswith("zip") or "getrelease/zip" in url else ".tar.xz"
            archive = Path(tmp) / f"ffmpeg-download{len(got)}{suffix}"
            try:
                say(f"  {url}")
                _download(url, archive)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                warn(f"download failed: {exc}")
                continue
            got.update(_extract_binaries(archive, wanted, FFMPEG_DIR))

    if not IS_WINDOWS:
        for name in got:
            target = FFMPEG_DIR / name
            try:
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass

    missing = wanted - got
    if missing:
        warn(f"could not extract: {', '.join(sorted(missing))}")
        return False
    say(f"{mark('ok')} ffmpeg and ffprobe installed into .ffmpeg/")
    return True


def ensure_ffmpeg(auto_install: bool = True) -> "tuple[Path, Path]":
    """Return (ffmpeg, ffprobe), downloading a static build if they are absent."""
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    if not auto_install:
        die("ffmpeg/ffprobe not found. Run 'python motionkit.py doctor' to install them.")
    if bootstrap_ffmpeg():
        ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not (ffmpeg and ffprobe):
        die("ffmpeg/ffprobe unavailable and automatic install failed — install ffmpeg manually.")
    return ffmpeg, ffprobe


# ── json / state ─────────────────────────────────────────────────────────────

def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"could not read {path.name}: {exc}")
        return default


def write_json(path: Path, data) -> None:
    """Write via a temp file and os.replace, which is atomic on every platform.

    A half-written state.json loses the spend log, and the spend log is the only
    record of what the user was actually charged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def rel_posix(path: Path, base: Path) -> str:
    """Relative path with forward slashes — for anything crossing into JSON or HTML.

    A stringified Windows Path gives ``frames\\hero\\desktop\\…``, which 404s in a
    browser. Every path that leaves Python goes through here.
    """
    return Path(os.path.relpath(str(path), str(base))).as_posix()


@contextlib.contextmanager
def project_lock(directory: Path, timeout: float = 30.0):
    """Serialise read-modify-write on a project's state.json.

    Phase 7 of the consultation runs clips in parallel, so two processes will
    race on the same file and one entire spend entry would be lost. There is no
    fcntl on Windows; O_CREAT|O_EXCL is atomic there and everywhere else. A lock
    older than two minutes is treated as abandoned.
    """
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".lock"
    deadline = time.monotonic() + timeout
    handle = None
    while True:
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > 120
            except OSError:
                stale = False
            if stale:
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            if time.monotonic() > deadline:
                die(f"another motionkit process is holding {lock}. "
                    f"If nothing else is running, delete that file.")
            time.sleep(0.15)
    try:
        with contextlib.suppress(OSError):
            os.write(handle, str(os.getpid()).encode("ascii"))
        yield
    finally:
        with contextlib.suppress(OSError):
            os.close(handle)
        with contextlib.suppress(OSError):
            lock.unlink()


def project_dir(name: str) -> Path:
    return OUT_DIR / name


def require_project(name: str) -> Path:
    directory = project_dir(name)
    if not directory.exists():
        die(f"no such project '{name}' — run: python motionkit.py init {name}")
    return directory


def new_state() -> dict:
    #: `jobs` is not in the spec. It holds in-flight provider request ids so an
    #: interrupted run resumes an existing job instead of paying for a second one.
    return {"phase": "intake", "approvals": {}, "spend": [], "assets": {}, "jobs": {}}


def load_state(name: str) -> dict:
    state = read_json(project_dir(name) / "state.json", None)
    if state is None:
        return new_state()
    for key, value in new_state().items():
        state.setdefault(key, value)
    return state


def save_state(name: str, state: dict) -> None:
    write_json(project_dir(name) / "state.json", state)


@contextlib.contextmanager
def mutate_state(name: str):
    """Read-modify-write state.json under the project lock.

    The read has to happen inside the lock too, or two writers each start from
    the same snapshot and the second one silently discards the first's entry.
    """
    with project_lock(project_dir(name)):
        state = load_state(name)
        yield state
        save_state(name, state)


def load_project_config(name: str) -> dict:
    return read_json(project_dir(name) / "project.json", {}) or {}


def total_spend(state: dict) -> float:
    return round(sum(float(entry.get("usd", 0) or 0) for entry in state.get("spend", [])), 4)


# ── providers ────────────────────────────────────────────────────────────────

def load_providers() -> "dict[str, dict]":
    """Read providers/*.json. Model IDs live here, never in this file."""
    providers: "dict[str, dict]" = {}
    if not PROVIDERS_DIR.exists():
        return providers
    for path in sorted(PROVIDERS_DIR.glob("*.json")):
        config = read_json(path, None)
        if isinstance(config, dict):
            providers[path.stem] = config
    return providers


def load_provider(name: str) -> dict:
    providers = load_providers()
    if name not in providers:
        known = ", ".join(sorted(providers)) or "none found"
        die(f"unknown provider '{name}' (available: {known})")
    return providers[name]


def provider_key(config: dict) -> "str | None":
    env_name = config.get("key_env")
    if not env_name:
        return None
    value = os.environ.get(env_name, "").strip()
    return value or None


def redact(secret: str) -> str:
    """Never print a key in full — and Gemini puts one in a download URL."""
    if not secret:
        return ""
    return f"{secret[:4]}…{secret[-4:]}" if len(secret) > 12 else "set"


def resolve_model(config: dict, project_config: dict, op: str,
                  cli_model: "str | None" = None) -> "tuple[str, str]":
    """Pick a model for `op`: --model, then project.json, then the provider default.

    A --model value naming a catalogue key is resolved through the JSON; anything
    else is passed through as a literal id with a warning, because it bypasses
    the pricing table. Model ids themselves never appear in this file.
    """
    catalog = config.get("models") or {}
    override = (project_config.get("models") or {}).get(op)
    choice = cli_model or override or op

    if choice in catalog:
        model_id = catalog[choice]
        if not model_id:
            die(f"{config.get('name', '?')} has no {op} model — "
                f"see {config.get('catalog_url', 'the provider catalogue')}")
        return choice, model_id

    if cli_model:
        warn(f"'{cli_model}' is not a key in {config.get('name', '?')}'s catalogue; "
             f"using it as a literal model id, which bypasses the pricing table")
        return cli_model, cli_model

    die(f"no '{op}' model configured for {config.get('name', '?')}")


def op_fields(config: dict, op: str, model_id: str) -> "dict | None":
    """Logical field name -> wire field name, for one operation and model.

    None means the provider cannot do this operation at all. Per-model overrides
    are matched by glob against the model id, so a model whose parameter differs
    — Kling wants start_image_url where Seedance wants image_url — is a config
    edit rather than a Python change.
    """
    fields = (config.get("fields") or {}).get(op)
    if fields is None:
        return None
    merged = dict(fields)
    for pattern, override in (config.get("model_fields") or {}).items():
        if fnmatch.fnmatch(model_id, pattern):
            merged.update(override.get(op) or {})
    return merged


def estimate_usd(config: dict, op: str, model_key: str,
                 params: "dict | None" = None) -> "tuple[float, str]":
    """Estimated cost plus the arithmetic behind it, printed before every paid call.

    Always an estimate from local JSON, never an invoice — provider rates drift.
    """
    pricing = dict(config.get("pricing") or {})
    pricing.update((pricing.get("by_model_key") or {}).get(model_key) or {})
    params = params or {}

    if op == "image":
        usd = float(pricing.get("image_usd") or 0.0)
        return round(usd, 4), f"1 image x ${usd:.4f}"
    if op == "cutout":
        usd = float(pricing.get("cutout_usd") or 0.0)
        return round(usd, 4), f"1 cutout x ${usd:.4f}"
    if op == "video":
        rate = float(pricing.get("video_usd_per_second") or 0.0)
        seconds = float(params.get("duration") or 0.0)
        return round(rate * seconds, 4), f"{seconds:g}s x ${rate:.4f}/s"
    return 0.0, "no charge"


def clamp_duration(config: dict, seconds: float) -> "tuple[float, bool]":
    """Clamp to the provider's ceiling and snap to its allowed steps, warning if changed."""
    limits = config.get("limits") or {}
    original = seconds
    ceiling = limits.get("max_seconds")
    if ceiling and seconds > float(ceiling):
        seconds = float(ceiling)
    steps = limits.get("duration_steps")
    if steps:
        seconds = min((float(s) for s in steps), key=lambda s: (abs(s - seconds), s))
    changed = abs(seconds - original) > 1e-9
    if changed:
        warn(f"{config.get('name', '?')} accepts {limits.get('duration_steps') or f'up to {ceiling}s'}"
             f" — {original:g}s becomes {seconds:g}s")
    return seconds, changed


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_doctor(args: argparse.Namespace) -> int:
    rule("environment")
    say(f"  platform    {platform.system()} {platform.release()} ({platform.machine()})")
    say(f"  python      {sys.version.split()[0]}  [{sys.executable}]")
    say(f"  motion-kit  {__version__}  [{ROOT}]")
    say(f"  console     {sys.stdout.encoding} (unicode: {'yes' if _UNICODE else 'ascii fallback'})")

    rule("ffmpeg")
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not (ffmpeg and ffprobe) and not args.no_install:
        bootstrap_ffmpeg()
        ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    for label, tool in (("ffmpeg ", ffmpeg), ("ffprobe", ffprobe)):
        if tool:
            say(f"  {mark('ok')} {label}   {tool}")
            say(f"              {tool_version(tool)}")
        else:
            say(f"  {mark('bad')} {label}   not found")

    rule("providers")
    providers = load_providers()
    if not providers:
        say(f"  {mark('bad')} no provider configs in providers/")
    usable = []
    for name, config in sorted(providers.items()):
        title = config.get("name", name)
        if not config.get("generation", True):
            say(f"  {mark('ok')} {name:<8} {title} — no key needed, bring your own footage")
            usable.append(name)
            continue
        env_name = config.get("key_env", "?")
        key = provider_key(config)
        if key:
            say(f"  {mark('ok')} {name:<8} {title} — {env_name} set ({redact(key)})")
            usable.append(name)
        else:
            say(f"  {mark('bad')} {name:<8} {title} — {env_name} missing")
            if config.get("key_url"):
                say(f"              get one at {config['key_url']}")
        if config.get("verified") is False:
            say(f"              {mark('warn')} unverified adapter — see verified_note "
                f"in providers/{name}.json")

    rule("projects")
    projects = sorted(p for p in OUT_DIR.glob("*") if p.is_dir()) if OUT_DIR.exists() else []
    if not projects:
        say(f"  {mark('dot')} none yet — run: python motionkit.py init <project>")
    for path in projects:
        state = load_state(path.name)
        spent = total_spend(state)
        say(f"  {mark('dot')} {path.name:<20} phase={state.get('phase', '?'):<12} spent=${spent:.2f}")

    say()
    if not (ffmpeg and ffprobe):
        say("ffmpeg is required. Re-run doctor to retry the download, or install it yourself.")
        return 1
    if usable:
        say(f"Ready. Build with: {', '.join(usable)}")
    say("Next: python motionkit.py init <project>")
    return 0


BRIEF_TEMPLATE = """# {name} — brief

Written by Claude during intake (Phase 1). This file is an *output* of the
conversation and an optional *input* when resuming; it is never a prerequisite.

## What this is

## Who it is for

## What a visitor should do

## Constraints
- Commercial use:
- Likeness / IP:

## Notes
"""


def cmd_init(args: argparse.Namespace) -> int:
    name = args.project
    directory = project_dir(name)
    providers = load_providers()
    if providers and args.provider not in providers:
        die(f"unknown provider '{args.provider}' (available: {', '.join(sorted(providers))})")

    if directory.exists():
        state = load_state(name)
        if state.get("spend") and not args.force:
            spent = total_spend(state)
            die(
                f"project '{name}' already exists and has ${spent:.2f} of spend recorded.\n"
                f"  Re-running init would reset its scaffolding. Pass --force to refresh the\n"
                f"  site/ templates while keeping build/, the spend log and state."
            )
        if not args.force:
            die(f"project '{name}' already exists — pass --force to refresh its templates")
        say(f"{mark('warn')} refreshing '{name}' — build/, spend and state are preserved")
    else:
        state = new_state()

    (directory / "build").mkdir(parents=True, exist_ok=True)
    site = directory / "site"
    site.mkdir(parents=True, exist_ok=True)

    # kit/ is pristine; site/ is the per-project copy. Never edit kit/ per project.
    copied = 0
    if KIT_DIR.exists():
        for item in sorted(KIT_DIR.iterdir()):
            if item.is_file():
                shutil.copyfile(item, site / item.name)
                copied += 1
            elif item.is_dir():
                shutil.copytree(item, site / item.name, dirs_exist_ok=True)
                copied += 1
    else:
        warn("kit/ not found — site/ is empty. Templates land in a later build step.")

    brief = directory / "brief.md"
    if not brief.exists():
        brief.write_text(BRIEF_TEMPLATE.format(name=name), encoding="utf-8")

    config = load_project_config(name)
    provider_config = providers.get(args.provider, {})
    config.update({
        "name": name,
        "provider": args.provider,
        "created": config.get("created") or now_iso(),
        # Per-operation overrides by catalogue *key*, not model id. null means
        # "use the provider default", so pinning a tier for one project never
        # edits the shared provider file and no model id lands in project.json.
        "models": config.get("models") or {"image": None, "video": None, "cutout": None},
        "defaults": config.get("defaults") or dict(DEFAULTS),
    })
    write_json(directory / "project.json", config)
    save_state(name, state)

    say(f"{mark('ok')} initialised {directory}")
    say(f"  provider  {args.provider}")
    say(f"  copied    {copied} item(s) from kit/ into site/")
    say(f"  files     brief.md, project.json, state.json, build/, site/")
    if not provider_config.get("generation", True):
        say(f"\n  Bring-your-own: drop stills and mp4s into {directory / 'build'}")
    elif provider_config and not provider_key(provider_config):
        say(f"\n  {mark('warn')} {provider_config.get('key_env')} is not set — generation will fail")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    require_project(args.project)
    state = load_state(args.project)
    spend = state.get("spend", [])
    if not spend:
        say(f"No spend recorded for '{args.project}'. Total: $0.00")
        return 0

    rule(f"spend — {args.project}")
    say(f"  {'when':<21} {'kind':<8} {'usd':>8}  model / note")
    for entry in spend:
        when = str(entry.get("at", ""))[:19]
        kind = str(entry.get("kind", "?"))
        usd = float(entry.get("usd", 0) or 0)
        detail = str(entry.get("model", ""))
        if entry.get("note"):
            detail = f"{detail} — {entry['note']}" if detail else str(entry["note"])
        say(f"  {when:<21} {kind:<8} {usd:>8.4f}  {detail}")
    rule()
    say(f"  {'total':<21} {'':<8} {total_spend(state):>8.2f}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import http.server
    import socketserver

    directory = require_project(args.project) / "site"
    if not directory.exists():
        die(f"{directory} does not exist — run init first")

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        # Deliberately not setting allow_reuse_address: on Windows SO_REUSEADDR
        # lets an unrelated process hijack the port rather than just easing TIME_WAIT.

    class Handler(http.server.SimpleHTTPRequestHandler):
        # Threading plus keep-alive. A section is ~180 frame requests; the stdlib
        # default of single-threaded HTTP/1.0 with no keep-alive serialises them
        # and makes a working page look broken during QA.
        protocol_version = "HTTP/1.1"
        extensions_map = dict(http.server.SimpleHTTPRequestHandler.extensions_map)
        # Windows resolves these from the registry, where .webp and even .js
        # are routinely absent or wrong. A canvas silently fails to decode an
        # image served as application/octet-stream.
        extensions_map.update({
            ".webp": "image/webp", ".avif": "image/avif", ".jpg": "image/jpeg",
            ".png": "image/png", ".svg": "image/svg+xml",
            ".js": "text/javascript", ".mjs": "text/javascript",
            ".css": "text/css", ".json": "application/json",
            ".woff2": "font/woff2", ".mp4": "video/mp4",
        })

        def __init__(self, *handler_args, **handler_kwargs):
            super().__init__(*handler_args, directory=str(directory), **handler_kwargs)

        def log_message(self, fmt: str, *fmt_args) -> None:  # quieter than the default
            sys.stderr.write(f"  {self.address_string()} {fmt % fmt_args}\n")

    port = args.port
    for attempt in range(20):
        try:
            with Server(("127.0.0.1", port), Handler) as httpd:
                if attempt:
                    say(f"{mark('dot')} port {args.port} was busy")
                say(f"{mark('ok')} serving {directory}")
                say(f"  http://127.0.0.1:{port}/    (ctrl-c to stop)")
                try:
                    httpd.serve_forever()
                except KeyboardInterrupt:
                    say("\nstopped")
                return 0
        except OSError:
            port += 1
    die(f"no free port in {args.port}–{port}")
    return 1


# ── cli ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="motionkit",
        description="Build scroll-driven cinematic landing pages.",
    )
    parser.add_argument("--version", action="version", version=f"motion-kit {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    doctor = sub.add_parser("doctor", help="check platform, ffmpeg, provider keys and projects")
    doctor.add_argument("--no-install", action="store_true",
                        help="report missing ffmpeg instead of downloading it")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="scaffold out/<project>/")
    init.add_argument("project")
    init.add_argument("--provider", default="fal", help="fal, gemini or byo (default: fal)")
    init.add_argument("--force", action="store_true",
                      help="refresh templates in an existing project, keeping build/ and spend")
    init.set_defaults(func=cmd_init)

    cost = sub.add_parser("cost", help="itemised spend log and total")
    cost.add_argument("--project", required=True)
    cost.set_defaults(func=cmd_cost)

    serve = sub.add_parser("serve", help="serve site/ over http")
    serve.add_argument("--project", required=True)
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: "list[str] | None" = None) -> int:
    init_console()
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        say("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
