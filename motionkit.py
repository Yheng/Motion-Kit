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
import base64
import collections
import contextlib
import fnmatch
import hashlib
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
            hint = ("\n  Text-behind-subject needs a cutout, so that direction requires fal.\n"
                    "  Either switch provider for this one call or drop the cutout layer."
                    if op == "cutout" else "")
            die(f"{config.get('name', '?')} has no {op} model — "
                f"see {config.get('catalog_url', 'the provider catalogue')}{hint}", code=2)
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


# ── http ─────────────────────────────────────────────────────────────────────

POLL_INTERVAL = 6.0
POLL_CEILING = 40 * 60
RETRY_MAX = 5
HTTP_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 600
#: Over this, a still is downscaled before inlining. A 4K PNG is ~8 MB, which is
#: ~11 MB once base64'd, and bodies that size get rejected or time out.
DATA_URI_MAX_BYTES = 6 * 1024 * 1024

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def http_request(url: str, *, method: str = "GET", headers: "dict | None" = None,
                 body: "bytes | None" = None, timeout: float = HTTP_TIMEOUT,
                 retries: int = 0) -> "tuple[int, dict, bytes]":
    """One HTTP call.

    `retries` defaults to zero, and that default is the double-charge guard: a
    job-creating POST that times out may already have started a billable job,
    so only explicitly idempotent callers (polls, downloads) opt in.
    """
    attempt = 0
    while True:
        try:
            request = urllib.request.Request(url, data=body, method=method,
                                             headers=dict(headers or {}))
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if attempt < retries and exc.code in RETRY_STATUSES:
                _backoff(attempt, exc.headers.get("Retry-After") if exc.headers else None)
                attempt += 1
                continue
            return exc.code, dict(exc.headers or {}), payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                _backoff(attempt, None)
                attempt += 1
                continue
            die(f"network error calling {url.split('?')[0]}: {exc}")


def _backoff(attempt: int, retry_after: "str | None") -> None:
    delay = 2.0 ** attempt
    if retry_after:
        with contextlib.suppress(ValueError):
            delay = max(delay, float(retry_after))
    time.sleep(min(delay, 60.0))


def http_json(url: str, *, method: str = "GET", headers: "dict | None" = None,
              payload=None, timeout: float = HTTP_TIMEOUT,
              retries: int = 0) -> "tuple[int, object]":
    head = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        head.setdefault("Content-Type", "application/json")
    status, _, raw = http_request(url, method=method, headers=head, body=body,
                                  timeout=timeout, retries=retries)
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, {"_raw": raw.decode("utf-8", "replace")[:2000]}


def sniff_mime(data: bytes) -> str:
    """Identify by magic bytes, never by extension — a .png that is really a JPEG is common."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "image/avif" if data[8:12] in (b"avif", b"avis") else "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return "application/octet-stream"


def extension_for(mime: str) -> str:
    return {
        "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
        "image/avif": ".avif", "video/mp4": ".mp4", "video/webm": ".webm",
    }.get(mime, ".bin")


def data_uri(path: Path) -> str:
    """Inline a local image, which avoids a separate upload step and its lifecycle."""
    raw = path.read_bytes()
    if len(raw) > DATA_URI_MAX_BYTES:
        warn(f"{path.name} is {len(raw) / 1e6:.1f} MB; large bodies are often rejected. "
             f"Consider a smaller still.")
    return f"data:{sniff_mime(raw)};base64,{base64.b64encode(raw).decode('ascii')}"


def find_asset_url(obj) -> "str | None":
    """Breadth-first search for the first http url/uri anywhere in the response.

    Response shapes vary across models and change over time, so a fixed path
    would break on the next model rather than the next decade.
    """
    queue = collections.deque([obj])
    while queue:
        node = queue.popleft()
        if isinstance(node, dict):
            for key in ("url", "uri"):
                value = node.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


def find_inline_bytes(obj) -> "tuple[bytes, str] | None":
    """Some providers return base64 inline instead of a URL."""
    queue = collections.deque([obj])
    while queue:
        node = queue.popleft()
        if isinstance(node, dict):
            for key in ("data", "bytesBase64Encoded", "b64_json"):
                value = node.get(key)
                if isinstance(value, str) and len(value) > 256:
                    # Pad: some APIs return base64 without its trailing '='.
                    with contextlib.suppress(ValueError):
                        raw = base64.b64decode(value + "=" * (-len(value) % 4),
                                               validate=False)
                        if raw:
                            return raw, sniff_mime(raw)
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


def looks_moderated(config: dict, blob: str) -> bool:
    lowered = blob.lower()
    return any(p in lowered for p in (config.get("moderation_patterns") or []))


def quota_error(config: dict, model_id: str, body) -> "str | None":
    """Turn a 429 into something actionable.

    A 429 is not a bad request — the key is valid and the call shape was
    accepted. It means rate limit or, far more often, no billing on the account.
    Google reports the latter as 'limit: 0' on a free-tier metric, which is a
    different problem from 'slow down' and needs a different answer.
    """
    blob = json.dumps(body)
    hard = "limit: 0" in blob or "free_tier" in blob
    lines = [f"{config.get('name', '?')} returned 429 for '{model_id}'."]
    if hard:
        lines += [
            "  Your key is valid — this is a quota of ZERO, which means the account",
            "  has no paid billing enabled for this model. Free-tier access to it is 0,",
            "  so no amount of waiting or retrying will help.",
            f"  Enable billing, then retry: {config.get('billing_url', config.get('key_url', ''))}",
        ]
    else:
        lines += ["  This looks like a rate limit rather than a billing problem.",
                  "  Wait a minute and re-run the identical command."]
    lines.append("  Nothing was charged: no job was created.")
    return "\n".join(lines)


def job_fingerprint(provider: str, op: str, model_id: str, payload: dict) -> str:
    """Identity of a job, so an interrupted run resumes instead of paying twice."""
    canonical = json.dumps({"p": provider, "o": op, "m": model_id, "b": payload},
                           sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ── fal adapter ──────────────────────────────────────────────────────────────

def split_data_uri(uri: str) -> "tuple[str, str]":
    """`data:image/png;base64,AAA` -> ('AAA', 'image/png'). Gemini wants them apart."""
    header, _, encoded = uri.partition(",")
    mime = header[5:].split(";")[0] if header.startswith("data:") else "image/png"
    return encoded, mime


def fal_submit(config: dict, op: str, model_id: str, payload: dict) -> dict:
    key = provider_key(config)
    base = config.get("queue_base", "https://queue.fal.run").rstrip("/")
    # retries=0: a POST that times out may already have created a billable job.
    status, body = http_json(f"{base}/{model_id}", method="POST",
                             headers={"Authorization": f"Key {key}"},
                             payload=payload, retries=0)
    if status == 404 or (isinstance(body, dict) and "not found" in str(body).lower()):
        die(f"{config.get('name')} does not know the endpoint '{model_id}'.\n"
            f"  Model ids drift. Edit providers/fal.json — do not guess a replacement.\n"
            f"  Catalogue: {config.get('catalog_url', 'https://fal.ai/models')}", code=3)
    if status in (401, 403):
        die(f"{config.get('key_env')} was rejected ({status}). "
            f"Check the key at {config.get('key_url')}", code=3)
    if status == 429:
        die(quota_error(config, model_id, body), code=3)
    if status >= 400 or not isinstance(body, dict):
        die(f"{config.get('name')} refused the request ({status}): "
            f"{json.dumps(body)[:400]}", code=3)

    request_id = body.get("request_id")
    if not request_id:
        die(f"no request_id in the queue response: {json.dumps(body)[:400]}", code=3)
    return {
        "request_id": request_id,
        "status_url": body.get("status_url") or f"{base}/{model_id}/requests/{request_id}/status",
        "response_url": body.get("response_url") or f"{base}/{model_id}/requests/{request_id}",
    }


def fal_poll(config: dict, handle: dict) -> "tuple[str, dict]":
    key = provider_key(config)
    status, body = http_json(handle["status_url"],
                             headers={"Authorization": f"Key {key}"}, retries=RETRY_MAX)
    if status >= 400 or not isinstance(body, dict):
        # Not "still running": reporting it that way hides a terminal failure
        # behind the full 40-minute ceiling.
        return "error", {"http_status": status, "body": body}
    state = str(body.get("status", "")).upper()
    if state == "COMPLETED":
        return "done", body
    if state in ("FAILED", "ERROR", "CANCELLED"):
        return ("rejected" if looks_moderated(config, json.dumps(body)) else "failed"), body
    return "running", body


def fal_fetch(config: dict, handle: dict) -> "tuple[bytes, str]":
    key = provider_key(config)
    auth = {"Authorization": f"Key {key}"}
    status, body = http_json(handle["response_url"], headers=auth, retries=RETRY_MAX)
    if status >= 400:
        # fal marks a job COMPLETED in the queue even when it failed validation;
        # the real verdict only appears here. A 4xx is the job being refused,
        # not collection breaking, so it must not be billed.
        raise ProviderFailure(
            f"the provider refused the job ({status}):\n"
            f"  {json.dumps(body, indent=2)[:600]}",
            moderated=looks_moderated(config, json.dumps(body)),
        )
    if looks_moderated(config, json.dumps(body)):
        return b"", "rejected"

    url = find_asset_url(body)
    if url:
        _, _, raw = http_request(url, timeout=DOWNLOAD_TIMEOUT, retries=RETRY_MAX)
        return raw, sniff_mime(raw)
    inline = find_inline_bytes(body)
    if inline:
        return inline
    die(f"no asset url found anywhere in the response: {json.dumps(body)[:400]}", code=3)


# ── gemini adapter ───────────────────────────────────────────────────────────
#
# UNVERIFIED. These call shapes have never been exercised against the live API.
# Gemini uses long-running *operations* rather than fal's queue, returns image
# bytes inline rather than by URL, and needs the API key appended to the video
# download URI — so every printed URL goes through redact() first.

def _gemini_url(config: dict, path: str) -> str:
    base = config.get("api_base", "").rstrip("/")
    return f"{base}/{path.lstrip('/')}?key={provider_key(config)}"


def gemini_submit(config: dict, op: str, model_id: str, payload: dict) -> dict:
    method = (config.get("methods") or {}).get(op)
    if not method:
        die(f"providers/gemini.json has no method for '{op}'", code=2)

    if method == "generateContent":
        body = {
            "contents": [{"parts": [{"text": payload.get("prompt", "")}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        if payload.get("aspectRatio"):
            body["generationConfig"]["imageConfig"] = {"aspectRatio": payload["aspectRatio"]}
    else:
        instance: dict = {"prompt": payload.get("prompt", "")}
        for wire, target in (("image", "image"), ("lastFrame", "lastFrame")):
            if payload.get(wire):
                encoded, mime = split_data_uri(payload[wire])
                instance[target] = {"bytesBase64Encoded": encoded, "mimeType": mime}
        parameters = {k: payload[k] for k in ("durationSeconds", "aspectRatio", "resolution")
                      if payload.get(k) is not None}
        body = {"instances": [instance], "parameters": parameters}

    status, response = http_json(_gemini_url(config, f"models/{model_id}:{method}"),
                                 method="POST", payload=body, retries=0)

    if status == 404:
        die(f"Google does not know the model '{model_id}'.\n"
            f"  Model ids drift — Veo 3 and Veo 2 were retired on 30 June 2026.\n"
            f"  Edit providers/gemini.json; do not guess a replacement.\n"
            f"  Catalogue: {config.get('catalog_url')}", code=3)
    if status in (401, 403):
        die(f"{config.get('key_env')} was rejected ({status}). "
            f"Check it at {config.get('key_url')}", code=3)
    if status == 429:
        die(quota_error(config, model_id, response), code=3)
    if status >= 400 or not isinstance(response, dict):
        die(f"Gemini refused the request ({status}): {json.dumps(response)[:400]}", code=3)

    # generateContent answers immediately; predictLongRunning hands back an operation.
    if method == "generateContent":
        return {"inline": response, "request_id": None}
    name = response.get("name")
    if not name:
        die(f"no operation name in the response: {json.dumps(response)[:400]}", code=3)
    return {"operation": name, "request_id": name.rsplit("/", 1)[-1]}


def gemini_poll(config: dict, handle: dict) -> "tuple[str, dict]":
    if "inline" in handle:
        return "done", handle["inline"]

    status, body = http_json(_gemini_url(config, handle["operation"]), retries=RETRY_MAX)
    if status >= 400 or not isinstance(body, dict):
        return "running", {}
    if not body.get("done"):
        return "running", body
    if body.get("error"):
        blob = json.dumps(body)
        return ("rejected" if looks_moderated(config, blob) else "failed"), body
    return "done", body


def gemini_fetch(config: dict, handle: dict) -> "tuple[bytes, str]":
    _, body = (200, handle["inline"]) if "inline" in handle else \
        http_json(_gemini_url(config, handle["operation"]), retries=RETRY_MAX)

    if looks_moderated(config, json.dumps(body)):
        return b"", "rejected"

    inline = find_inline_bytes(body)
    if inline:
        return inline

    url = find_asset_url(body)
    if url:
        # The result URI needs the API key as a query parameter to download.
        signed = f"{url}{'&' if '?' in url else '?'}key={provider_key(config)}"
        _, _, raw = http_request(signed, timeout=DOWNLOAD_TIMEOUT, retries=RETRY_MAX)
        return raw, sniff_mime(raw)

    die(f"no image bytes or asset url in the response: {json.dumps(body)[:400]}", code=3)


ADAPTERS = {
    "fal": {"submit": fal_submit, "poll": fal_poll, "fetch": fal_fetch,
            "interval": POLL_INTERVAL},
    "gemini": {"submit": gemini_submit, "poll": gemini_poll, "fetch": gemini_fetch,
               "interval": 10.0},
}


# ── ffmpeg operations ────────────────────────────────────────────────────────

#: Placeholder clips match the canonical 6s render, so the fps arithmetic on the
#: dry-run path is identical to the real one.
PLACEHOLDER_SECONDS = 6.0
FRAME_GLOB = "frame_*"


def run_ffmpeg(ffmpeg: Path, args: "list[str]", label: str) -> None:
    """Run ffmpeg with an argv list. Never shell=True, never a quoted filter string."""
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-20:]
        die(f"ffmpeg failed while {label}:\n  " + "\n  ".join(tail) if tail
            else f"ffmpeg failed while {label} (exit {proc.returncode})")


def probe_duration(ffprobe: Path, clip: Path) -> float:
    proc = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(clip)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    raw = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        duration = float(raw[0].strip())
    except (IndexError, ValueError):
        detail = proc.stderr.decode("utf-8", "replace").strip()
        die(f"could not read a duration from {clip.name}"
            + (f":\n  {detail}" if detail else ""))
    if duration <= 0:
        die(f"{clip.name} reports a duration of {duration}s")
    return duration


def encoder_args(fmt: str, quality: int) -> "list[str]":
    """Encoder flags per output format. `quality` is 0-100, higher is better."""
    if fmt == "webp":
        # -compression_level is a generic codec option rather than a libwebp
        # private one, so it does not appear in -h encoder=libwebp but is accepted.
        return ["-c:v", "libwebp", "-quality", str(quality), "-compression_level", "6"]
    if fmt == "jpg":
        # mjpeg -q:v runs 2..31 with *lower* meaning better — the inverse of ours.
        scaled = min(31, max(2, round(2 + (100 - quality) * 0.29)))
        return ["-c:v", "mjpeg", "-q:v", str(scaled)]
    if fmt == "avif":
        crf = min(50, max(18, round(63 - 0.4 * quality)))
        # -still-picture is what makes each file a still (ftypavif) rather than a
        # one-frame sequence; without -f image2 alongside it, ffmpeg selects the
        # avif *sequence* muxer, ignores the %04d pattern entirely, and writes a
        # single animated file literally named frame_%04d.avif.
        return ["-c:v", "libaom-av1", "-crf", str(crf), "-cpu-used", "6",
                "-still-picture", "1"]
    die(f"unknown format '{fmt}' — use webp, jpg or avif")


def clear_frames(directory: Path) -> int:
    """Delete frames from a previous slice.

    Re-slicing 180 frames down to 120 otherwise leaves frame_0121..0180 behind,
    and a later re-slice at 200 would interleave two different takes.
    """
    if not directory.exists():
        return 0
    removed = 0
    for stale in directory.glob(FRAME_GLOB):
        if stale.is_file():
            stale.unlink()
            removed += 1
    return removed


def slice_frames(ffmpeg: Path, input_args: "list[str]", dest: Path, *,
                 count: int, width: int, fmt: str, quality: int,
                 duration: float, trim_end: float) -> int:
    """Slice one variant. Returns the number of files actually written.

    ffmpeg's fps filter rounds, so the written count routinely differs from
    `count` by one — the caller must use the returned number, never the request.
    """
    dest.mkdir(parents=True, exist_ok=True)
    clear_frames(dest)

    usable = duration - trim_end
    if usable <= 0:
        die(f"--trim-end {trim_end}s leaves nothing of a {duration:.2f}s clip")
    fps = count / usable

    args = list(input_args)
    args += ["-vf", f"fps={fps:.6f},scale={width}:-2:flags=lanczos", "-an"]
    args += encoder_args(fmt, quality)
    # Pin the image2 muxer so numbered output is guaranteed for every format
    # rather than inferred from the extension.
    args += ["-f", "image2"]
    if trim_end > 0:
        # Without this the fps filter resamples across the WHOLE input and the
        # trimmed tail is still emitted, just sampled faster: asking for 30
        # frames from a 3s clip with trim_end=1 yields 45, tail included.
        args += ["-t", f"{usable:.6f}"]
    args += [str(dest / f"frame_%04d.{fmt}")]

    run_ffmpeg(ffmpeg, args, f"slicing {dest.parent.name}/{dest.name}")
    return len(list(dest.glob(f"{FRAME_GLOB}.{fmt}")))


def write_poster(ffmpeg: Path, first_frame: Path, poster_dir: Path,
                 name: str, width: int) -> "list[Path]":
    """Write the LCP poster from frame 1 of the sequence.

    Deriving it from the same frame the canvas paints first is what makes the
    poster-to-canvas fade invisible; any other source guarantees a visible pop.
    """
    poster_dir.mkdir(parents=True, exist_ok=True)
    scale = f"scale={width}:-2:flags=lanczos"
    jpg = poster_dir / f"{name}.jpg"
    webp = poster_dir / f"{name}.webp"
    run_ffmpeg(ffmpeg, ["-i", str(first_frame), "-vf", scale, "-q:v", "3", str(jpg)],
               "writing the poster jpg")
    run_ffmpeg(ffmpeg, ["-i", str(first_frame), "-vf", scale,
                        "-c:v", "libwebp", "-quality", "82", str(webp)],
               "writing the poster webp")
    return [jpg, webp]


def probe_dimensions(ffprobe: Path, image: Path) -> "tuple[int, int]":
    proc = subprocess.run(
        [str(ffprobe), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(image)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    try:
        width, height = proc.stdout.decode("utf-8", "replace").strip().split("x")[:2]
        return int(width), int(height)
    except ValueError:
        die(f"could not read the dimensions of {image.name}")


def section_frames(project: str, name: str,
                   variant: str = "desktop") -> "tuple[Path, str, int]":
    """(directory, format, count) for an already-sliced section."""
    state = load_state(project)
    section = (state.get("sections") or {}).get(name)
    if not section:
        known = ", ".join(state.get("sections") or {}) or "none yet"
        die(f"section '{name}' has not been sliced (known: {known}).\n"
            f"  Run: python motionkit.py frames --project {project} --name {name}", code=2)
    variants = section.get("variants") or {}
    if variant not in variants:
        die(f"section '{name}' has no {variant} variant", code=2)
    directory = project_dir(project) / "site" / "frames" / name / variant
    if not directory.exists():
        die(f"{directory} is missing — frames are gitignored and cleared on "
            f"re-slice. Run `frames` again.", code=2)
    return directory, section.get("format", "webp"), int(variants[variant]["count"])


def cmd_contact(args: argparse.Namespace) -> int:
    """Tile evenly-spaced frames into one sheet, so the clip can be *looked at*.

    Nothing else in the pipeline shows what is at a given frame, which is why
    copy ends up naming nothing on screen. Arithmetic is not a substitute: the
    reference build's "360 turntable" was uneven, drifted sideways, and grew a
    doorway that was not in the approved still.
    """
    require_project(args.project)
    directory, fmt, count = section_frames(args.project, args.name)
    ffmpeg, _ = ensure_ffmpeg()

    cells = max(2, min(args.cells, count))
    cols = max(1, min(args.cols, cells))
    rows = -(-cells // cols)
    step = max(1, count // cells)

    out = project_dir(args.project) / "build" / "contact" / f"{args.name}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    # mod(n\,step): the comma is escaped because commas separate filters.
    run_ffmpeg(ffmpeg, [
        "-start_number", "1", "-i", str(directory / f"frame_%04d.{fmt}"),
        "-vf", f"select='not(mod(n\\,{step}))',scale={args.width}:-2,tile={cols}x{rows}",
        "-frames:v", "1", "-q:v", "3", str(out),
    ], f"building the contact sheet for '{args.name}'")

    picks = [i * step + 1 for i in range(cells) if i * step + 1 <= count]
    say(f"{mark('ok')} {rel_posix(out, project_dir(args.project))}"
        f"  ({len(picks)} cells, {cols}x{rows})")
    say(f"  {'cell':>4}  {'frame':>5}  {'progress':>8}")
    for index, frame in enumerate(picks, start=1):
        say(f"  {index:>4}  {frame:>5}  {(frame - 1) / max(count - 1, 1):>8.3f}")

    with mutate_state(args.project) as state:
        state["sections"][args.name]["contact"] = {
            "path": rel_posix(out, project_dir(args.project)),
            "cells": picks, "at": now_iso(),
        }
    say(f"\n  Read that image, then write one line per cell describing what is")
    say(f"  visible. Beats come from the observations, never from the arithmetic.")
    say(f"  $0.00 — no provider was called.")
    return 0


def cmd_pluck(args: argparse.Namespace) -> int:
    """Copy frames out of the sequence into site/still/ as page imagery.

    site/frames/ is gitignored and cleared by every re-slice, so a page cannot
    reference it directly — it would break on clone and on the next `frames`
    run. Plucking costs nothing: these frames are already paid for.
    """
    directory_project = require_project(args.project)
    directory, fmt, count = section_frames(args.project, args.name)
    ffmpeg, ffprobe = ensure_ffmpeg()

    try:
        numbers = [int(n) for n in str(args.frames).replace(" ", "").split(",") if n]
    except ValueError:
        die("--frames takes comma-separated frame numbers, e.g. 45,90,135", code=2)

    crop = None
    if args.crop:
        try:
            crop = [float(v) for v in str(args.crop).split(",")]
            assert len(crop) == 4 and all(0 <= v <= 1 for v in crop)
        except (ValueError, AssertionError):
            die("--crop takes four fractions of the source: x,y,w,h "
                "(e.g. 0.35,0.10,0.40,0.80)", code=2)

    out_format = args.format or fmt
    dest = directory_project / "site" / "still"
    dest.mkdir(parents=True, exist_ok=True)
    made = []

    for number in numbers:
        if not 1 <= number <= count:
            die(f"frame {number} is outside '{args.name}' (1–{count})", code=2)
        source = directory / f"frame_{number:04d}.{fmt}"
        if not source.exists():
            die(f"{source} is missing — re-slice with `frames`", code=2)

        chain = []
        if crop:
            width, height = probe_dimensions(ffprobe, source)
            # Fractions, not pixels, so a crop survives a re-slice at a
            # different --width.
            chain.append("crop={}:{}:{}:{}".format(
                max(2, int(width * crop[2])), max(2, int(height * crop[3])),
                int(width * crop[0]), int(height * crop[1])))
        chain.append(f"scale={args.width}:-2:flags=lanczos")

        out = dest / f"{args.name}_{number:04d}.{out_format}"
        run_ffmpeg(ffmpeg, ["-i", str(source), "-vf", ",".join(chain)]
                   + encoder_args(out_format, args.quality) + [str(out)],
                   f"plucking frame {number}")
        made.append(out)

    with mutate_state(args.project) as state:
        for out, number in zip(made, numbers):
            state["assets"][out.stem] = {
                "kind": "still", "path": rel_posix(out, directory_project),
                "bytes": out.stat().st_size, "at": now_iso(),
                "provider": None, "model": None,
                "params": {"section": args.name, "frame": number,
                           "crop": args.crop, "width": args.width},
                "prompt": None, "usd": 0.0, "job_id": None,
                "derived_from": args.name, "approved": False,
            }

    total = sum(p.stat().st_size for p in made) / 1e6
    for out in made:
        say(f"  {mark('ok')} {rel_posix(out, directory_project / 'site')}")
    say(f"  {len(made)} image(s), {total:.2f} MB")
    say(f"  $0.00 — extracted from frames you already paid for")
    return 0


def scrub_sections_snippet(sections: dict) -> str:
    """Paste-ready config, built from counts measured on disk."""
    entries = []
    for name, section in sections.items():
        variants = section.get("variants", {})
        counts = ", ".join(f'{v}: {variants[v]["count"]}'
                           for v in ("desktop", "mobile") if v in variants)
        fmt = section.get("format", "webp")
        entries.append(
            f'  {{\n'
            f'    section: "#{name}",\n'
            f'    name: "{name}",\n'
            f'    frameCount: {{ {counts} }},\n'
            f'    format: "{fmt}",\n'
            f'    framePath: (n, v) =>\n'
            f'      `frames/{name}/${{v}}/frame_${{String(n).padStart(4, "0")}}.{fmt}`,\n'
            f'  }}'
        )
    return "window.SCRUB_SECTIONS = [\n" + ",\n".join(entries) + ",\n];"


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
    #
    # Two classes of file, and the distinction is the spec's own: styles.css and
    # scrub.js are pristine scaffolding that is never edited per project, so they
    # ALWAYS refresh and a project picks up engine and layout fixes. index.html
    # and brand.css carry the copy and the direction — they are the deliverable,
    # so once they differ they are preserved and named.
    #
    # Comparing bytes alone cannot tell "this project edited it" from "the kit
    # moved on", which is why the pristine set is explicit rather than inferred.
    copied, kept = 0, []
    if KIT_DIR.exists():
        for item in sorted(KIT_DIR.iterdir()):
            target = site / item.name
            if item.is_file():
                pristine = item.name in ("styles.css", "scrub.js")
                if (not pristine and target.exists()
                        and target.read_bytes() != item.read_bytes()):
                    kept.append(item.name)
                    continue
                shutil.copyfile(item, target)
                copied += 1
            elif item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
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
    if kept:
        say(f"  kept      {', '.join(kept)} — edited for this project, not overwritten")
    say(f"  files     brief.md, project.json, state.json, build/, site/")
    if not provider_config.get("generation", True):
        say(f"\n  Bring-your-own: drop stills and mp4s into {directory / 'build'}")
    elif provider_config and not provider_key(provider_config):
        say(f"\n  {mark('warn')} {provider_config.get('key_env')} is not set — generation will fail")
    return 0


def cmd_phase(args: argparse.Namespace) -> int:
    """Record consultation progress. Without this the skill cannot resume."""
    require_project(args.project)

    if not (args.set or args.approve):
        state = load_state(args.project)
        say(f"  phase     {state.get('phase')}")
        approvals = state.get("approvals") or {}
        if approvals:
            for gate, record in approvals.items():
                choice = f" choice={record.get('choice')}" if record.get("choice") else ""
                say(f"  approved  {gate}{choice}  {record.get('at')}")
                if record.get("note"):
                    say(f"            {record['note']}")
        else:
            say("  approved  nothing yet")
        return 0

    if args.set and args.set not in PHASES:
        die(f"'{args.set}' is not a phase. One of: {', '.join(PHASES)}", code=2)

    with mutate_state(args.project) as state:
        if args.set:
            state["phase"] = args.set
            state.setdefault("phase_history", []).append({"phase": args.set, "at": now_iso()})
            say(f"{mark('ok')} phase = {args.set}")
        if args.approve:
            state.setdefault("approvals", {})[args.approve] = {
                "approved": True, "at": now_iso(),
                "choice": args.choice, "note": args.note or "",
            }
            say(f"{mark('ok')} approved {args.approve}"
                + (f" (choice {args.choice})" if args.choice else ""))
    return 0


#: Loud moves. A band may carry two; the peak band is the sole exception at three.
LOUD = ("band--invert", "band--field", "band--bleed")


def cmd_check(args: argparse.Namespace) -> int:
    """Free gate. Everything here is a defect this tool has actually shipped."""
    import re

    project = require_project(args.project)
    site = project / "site"
    page = site / "index.html"
    if not page.exists():
        die(f"{page} is missing", code=2)

    html = page.read_text(encoding="utf-8", errors="replace")
    css = ""
    for name in ("styles.css", "brand.css"):
        if (site / name).exists():
            css += (site / name).read_text(encoding="utf-8", errors="replace")

    live = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    body = live.split("<body", 1)[-1]
    results: "list[tuple[str, str, str]]" = []

    def record(level: str, label: str, detail: str = "") -> None:
        results.append((level, label, detail))

    # ── frame counts vs files on disk ────────────────────────────────────────
    # The one that fails silently: img.onerror resolves, so an over-count just
    # never paints the last frames.
    state = load_state(args.project)
    for name, section in (state.get("sections") or {}).items():
        for variant, meta in (section.get("variants") or {}).items():
            directory = site / "frames" / name / variant
            on_disk = len(list(directory.glob(f"{FRAME_GLOB}.{section.get('format','webp')}")))
            declared = re.search(
                r"frameCount:\s*\{[^}]*\b" + variant + r":\s*(\d+)", html)
            declared_n = int(declared.group(1)) if declared else None
            if declared_n is None:
                record("warn", f"{name}/{variant} count", "not declared in SCRUB_SECTIONS")
            elif declared_n != on_disk:
                record("fail", f"{name}/{variant} count",
                       f"page says {declared_n}, {on_disk} files on disk — the engine "
                       f"will request frames that do not exist, and fail silently")
            else:
                record("ok", f"{name}/{variant} count", f"{on_disk} frames")

    # ── unresolved claims ────────────────────────────────────────────────────
    # Whole document, not just the body: a {{claim}} in <title> shows in the
    # browser tab.
    claims = re.findall(r"\{\{[^}]*\}\}", live)
    if claims:
        record("fail", "unresolved {{claims}}",
               f"{len(claims)} still rendered to the visitor, e.g. {claims[0]}")
    else:
        record("ok", "unresolved {{claims}}", "none")

    # ── grounds and composition budgets ──────────────────────────────────────
    sections = re.findall(r'<section[^>]*id="([^"]+)"[^>]*class="([^"]+)"', body)
    grounds = []
    for sid, cls in sections:
        classes = set(cls.split())
        ground = ("invert" if "band--invert" in classes
                  else "field" if "band--field" in classes
                  else "scrub" if "scrub" in classes else "base")
        grounds.append((sid, ground))
    distinct = {g for _, g in grounds if g != "scrub"}
    if len(distinct) < 2 and len(grounds) > 2:
        record("fail", "ground rhythm",
               "every band is the same ground — this is the 'plain white page' defect")
    else:
        record("ok", "ground rhythm", f"{len(distinct)} distinct grounds")

    adjacent = [a for (a, ga), (b, gb) in zip(grounds, grounds[1:])
                if ga == gb and ga not in ("scrub", "base")]
    if adjacent:
        record("warn", "adjacent same ground", ", ".join(adjacent))

    for label, needle, budget in (("--bleed bands", "band--bleed", 1),
                                  (".statement", 'class="statement"', 1)):
        n = body.count(needle)
        record("ok" if n <= budget else "fail", f"{label} budget",
               f"{n} (max {budget})")

    for sid, cls in sections:
        classes = set(cls.split())
        chunk = body.split(f'id="{sid}"', 1)[-1].split("</section>", 1)[0]
        loud = len(classes & set(LOUD)) + ("statement" in chunk) + ("stat-row" in chunk)
        if loud > 3:
            record("fail", f"#{sid} loud moves", f"{loud} — the peak band's limit is 3")
        elif loud == 3:
            record("ok", f"#{sid} loud moves", "3 — this is the peak band")

    # ── imagery below the hero ───────────────────────────────────────────────
    after_hero = body.split("</section>", 1)[-1]
    below = len(re.findall(r"<img\b", after_hero))
    record("ok" if below else "fail", "images below the hero",
           f"{below}" if below else "none — `pluck` extracts them for $0")

    # ── headings ─────────────────────────────────────────────────────────────
    h1s = len(re.findall(r"<h1\b", body))
    record("ok" if h1s == 1 else "fail", "single h1", str(h1s))
    levels = [int(n) for n in re.findall(r"<h([1-6])\b", body)]
    jumps = [f"h{a}->h{b}" for a, b in zip(levels, levels[1:]) if b > a + 1]
    record("ok" if not jumps else "fail", "heading order",
           ", ".join(jumps) if jumps else "monotonic")

    # ── hero copy visible on arrival ─────────────────────────────────────────
    stage = body.split('class="stage"', 1)[-1].split("</section>", 1)[0]
    bad_in = re.findall(r'data-in="\s*0*\.?0+[\s"]', stage)
    record("ok" if not bad_in else "fail", "hero data-in at 0",
           "none" if not bad_in else f"{len(bad_in)} line(s) invisible on arrival")

    # ── beats ────────────────────────────────────────────────────────────────
    beats = []
    for raw in re.findall(r'data-beat="([^"]+)"', stage):
        parts = raw.split()[:2]
        if len(parts) == 2 and all(v.lstrip("-").isdigit() for v in parts):
            beats.append((int(parts[0]), int(parts[1])))
        else:
            record("warn", "data-beat", f'"{raw}" is not two frame numbers')
    if beats:
        beats.sort()
        gaps = [f"{a[1]}-{b[0]}" for a, b in zip(beats, beats[1:]) if b[0] - a[1] > 12]
        record("ok" if not gaps else "warn", "beat coverage",
               f"{len(beats)} beats" + (f", dead stretch at frames {', '.join(gaps)}"
                                        if gaps else ", no dead stretch"))

    # ── poster ───────────────────────────────────────────────────────────────
    preload = re.search(r'rel="preload"[^>]*href="([^"]+)"', html)
    if preload:
        target = site / preload.group(1)
        record("ok" if target.exists() else "fail", "poster preload",
               preload.group(1) if target.exists() else f"{preload.group(1)} is missing")

    # ── classes with no CSS ──────────────────────────────────────────────────
    used = set()
    for match in re.findall(r'class="([^"]+)"', body):
        used.update(c for c in match.split() if not c.startswith("{{"))
    unstyled = sorted(c for c in used if f".{c}" not in css)
    record("ok" if not unstyled else "warn", "classes with no CSS",
           ", ".join(unstyled) if unstyled else "none")

    # ── report ───────────────────────────────────────────────────────────────
    rule(f"check — {args.project}")
    glyph = {"ok": mark("ok"), "warn": mark("warn"), "fail": mark("bad")}
    for level, label, detail in results:
        say(f"  {glyph[level]} {label:<26} {detail}")
    failed = sum(1 for level, _, _ in results if level == "fail")
    warned = sum(1 for level, _, _ in results if level == "warn")
    say()
    say(f"  {failed} failed, {warned} warning(s), "
        f"{len(results) - failed - warned} passed")
    return 1 if failed else 0


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


class ProviderFailure(Exception):
    """A job the provider accepted and then refused. Never billed."""

    def __init__(self, message: str, moderated: bool = False):
        super().__init__(message)
        self.moderated = moderated


def cast_value(value, kind: "str | None"):
    """Coerce to the wire type named in the provider JSON.

    Seedance rejects duration 6.0 and requires the string "6"; Veo wants the
    integer 6. Neither belongs in Python, so the type lives beside the name.
    """
    if kind is None or value is None:
        return value
    if kind == "str":
        # 6.0 -> "6", not "6.0", which is what an enum-of-strings expects.
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value)
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    if kind == "bool":
        return bool(value)
    warn(f"unknown cast '{kind}' in providers/*.json — sending the value unchanged")
    return value


def field_spec(entry) -> "tuple[str | None, str | None]":
    """A field entry is a wire name, or {name, cast} when the type matters."""
    if entry is None:
        return None, None
    if isinstance(entry, str):
        return entry, None
    return entry.get("name"), entry.get("cast")


def build_payload(config: dict, op: str, model_id: str, params: dict) -> dict:
    """Map the CLI's own vocabulary onto this model's wire field names."""
    fields = op_fields(config, op, model_id)
    if fields is None:
        die(f"{config.get('name')} has no {op} model. "
            f"Text-behind directions need a provider that does.", code=2)

    payload: dict = {}
    for logical, value in params.items():
        if value is None:
            continue
        if logical not in fields:
            warn(f"{model_id}: no mapping for '{logical}' in providers/*.json — dropping it")
            continue
        wire, kind = field_spec(fields[logical])
        if wire is None:
            warn(f"{model_id} has no '{logical}' parameter — ignoring it")
            continue
        payload[wire] = cast_value(value, kind)
    return payload


def run_generation(project: str, op: str, *, out_name: str, params: dict,
                   cli_model: "str | None", note: str,
                   derived_from: "str | None" = None,
                   overwrite: bool = False) -> Path:
    """Submit, poll, collect and record one paid job.

    Everything that is not provider-specific lives here: the estimate printed
    before spending, the in-flight record that makes a crash resumable rather
    than expensive, and the spend row.
    """
    directory = require_project(project)
    project_config = load_project_config(project)
    config = load_provider(project_config.get("provider", "fal"))

    if not config.get("generation", True):
        die(f"{config.get('name')} does not generate anything.\n"
            f"  Put your own file at {directory / 'build' / out_name} and carry on —\n"
            f"  any clip with continuous camera motion works, and everything\n"
            f"  downstream of build/ is free.", code=2)

    adapter = ADAPTERS.get(config.get("kind", ""))
    if adapter is None:
        die(f"no adapter for provider kind '{config.get('kind')}'", code=2)

    key = provider_key(config)
    if not key:
        die(f"{config.get('key_env')} is not set — add it to .env.\n"
            f"  Get one at {config.get('key_url')}", code=2)

    model_key, model_id = resolve_model(config, project_config, op, cli_model)

    if params.get("duration") is not None:
        params["duration"], _ = clamp_duration(config, float(params["duration"]))

    out_path = directory / "build" / out_name
    if out_path.exists() and not overwrite:
        die(f"{out_path} already exists. Choose another --out, or pass --overwrite.\n"
            f"  Refusing by default because re-rendering costs money.", code=2)

    payload = build_payload(config, op, model_id, params)
    usd, breakdown = estimate_usd(config, op, model_key, params)
    state = load_state(project)
    running = total_spend(state)

    # The estimate is always printed before anything is submitted.
    rule(f"{op} — {config.get('name')}")
    say(f"  model     {model_id}")
    say(f"  estimate  ${usd:.4f}   ({breakdown})")
    say(f"  project   ${running:.2f} spent so far → ${running + usd:.2f} if this succeeds")
    if config.get("verified") is False:
        warn("this adapter has never been run against the live API; expect surprises")
    for notice in config.get("notices") or []:
        say(f"  note      {notice}")

    fingerprint = job_fingerprint(config.get("kind", "?"), op, model_id, payload)
    existing = (state.get("jobs") or {}).get(fingerprint)

    if existing:
        say(f"  {mark('warn')} an identical job was already submitted at "
            f"{existing.get('submitted')} — resuming it rather than paying again")
        handle = existing["handle"]
    else:
        say(f"  {mark('dot')} submitting…")
        handle = adapter["submit"](config, op, model_id, payload)
        # Recorded BEFORE the first poll. If this process dies mid-poll the job
        # keeps running and is still billed; without this record a re-run would
        # submit a second one and pay twice.
        with mutate_state(project) as mutable:
            mutable.setdefault("jobs", {})[fingerprint] = {
                "handle": handle, "op": op, "model": model_id,
                "estimate_usd": usd, "out": out_name, "submitted": now_iso(),
            }

    started = time.monotonic()
    outcome, response = "running", {}
    consecutive_errors = 0
    while True:
        outcome, response = adapter["poll"](config, handle)
        if outcome == "error":
            # Transient by assumption, but not forever: give up well before the
            # ceiling rather than pretending a broken poll is a running job.
            consecutive_errors += 1
            if consecutive_errors >= 10:
                say(f"\n  {mark('warn')} the status endpoint has failed 10 times "
                    f"({response.get('http_status')}). Leaving the job on record.")
                say(f"  Re-run the identical command to resume "
                    f"(request {handle.get('request_id')}).")
                return out_path
            time.sleep(adapter["interval"])
            continue
        consecutive_errors = 0
        if outcome != "running":
            break
        if time.monotonic() - started > POLL_CEILING:
            say(f"\n  {mark('warn')} still running after {POLL_CEILING // 60} minutes. "
                f"The job is NOT cancelled and may still bill.")
            say(f"  Re-run the identical command to resume polling "
                f"(request {handle.get('request_id')}).")
            return out_path
        time.sleep(adapter["interval"])

    elapsed = time.monotonic() - started

    if outcome in ("failed", "rejected"):
        with mutate_state(project) as mutable:
            (mutable.get("jobs") or {}).pop(fingerprint, None)
            mutable["spend"].append({
                "at": now_iso(), "kind": op, "provider": config.get("kind"),
                "model": model_id, "usd": 0.0, "billable": False,
                "estimated": True, "elapsed_s": round(elapsed, 1),
                "job_id": handle.get("request_id"), "asset": None,
                "note": f"{outcome.upper()} — not billed. {note}".strip(),
            })
        detail = json.dumps(response)[:400]
        if outcome == "rejected":
            die(f"the provider's moderation rejected this prompt. You were NOT billed.\n"
                f"  Rephrasing usually works: 'bursts outward in slow motion' passes\n"
                f"  where 'explodes' does not.\n  {detail}", code=4)
        die(f"the job failed. You were NOT billed.\n  {detail}", code=3)

    try:
        raw, mime = adapter["fetch"](config, handle)
    except ProviderFailure as failure:
        # The queue said COMPLETED but the result is a refusal. Record it
        # honestly as unbilled and clear the job so a corrected re-run is clean.
        with mutate_state(project) as mutable:
            (mutable.get("jobs") or {}).pop(fingerprint, None)
            mutable["spend"].append({
                "at": now_iso(), "kind": op, "provider": config.get("kind"),
                "model": model_id, "usd": 0.0, "billable": False,
                "estimated": True, "elapsed_s": round(elapsed, 1),
                "job_id": handle.get("request_id"), "asset": None,
                "note": f"REFUSED — not billed. {note}".strip(),
            })
        if failure.moderated:
            die(f"moderation rejected this prompt. You were NOT billed.\n"
                f"  Rephrasing usually works: 'bursts outward in slow motion'\n"
                f"  passes where 'explodes' does not.\n  {failure}", code=4)
        die(f"You were NOT billed.\n  {failure}", code=3)

    if mime == "rejected" or not raw:
        die("the provider returned no asset (moderation). You were NOT billed.", code=4)

    if not out_path.suffix:
        out_path = out_path.with_suffix(extension_for(mime))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, out_path)

    stem = out_path.stem
    with mutate_state(project) as mutable:
        (mutable.get("jobs") or {}).pop(fingerprint, None)
        mutable["spend"].append({
            "at": now_iso(), "kind": op, "provider": config.get("kind"),
            "model": model_id, "usd": usd, "billable": True, "estimated": True,
            "elapsed_s": round(elapsed, 1), "job_id": handle.get("request_id"),
            "asset": stem, "note": note,
        })
        mutable["assets"][stem] = {
            "kind": op, "path": rel_posix(out_path, directory),
            "bytes": len(raw), "at": now_iso(),
            "provider": config.get("kind"), "model": model_id,
            "params": {k: v for k, v in params.items() if k != "start_image"},
            "prompt": params.get("prompt"), "usd": usd,
            "job_id": handle.get("request_id"), "derived_from": derived_from,
            "approved": False,
        }
        new_total = total_spend(mutable)

    say(f"  {mark('ok')} {out_path.name}  {len(raw) / 1e6:.2f} MB in {elapsed:.0f}s")
    say(f"  cost      ${usd:.4f} this call, ${new_total:.2f} on this project so far")
    return out_path


def cmd_image(args: argparse.Namespace) -> int:
    run_generation(
        args.project, "image", out_name=args.out, cli_model=args.model,
        note=args.note or "", overwrite=args.overwrite,
        params={"prompt": args.prompt, "aspect": args.aspect, "resolution": args.resolution},
    )
    return 0


def cmd_video(args: argparse.Namespace) -> int:
    directory = require_project(args.project)
    still = Path(args.image)
    for option in (still, directory / still, directory / "build" / still.name):
        if option.is_file():
            still = option
            break
    else:
        die(f"no still at {args.image}")

    params = {
        "prompt": args.prompt,
        "start_image": data_uri(still),
        "duration": args.duration,
        "resolution": args.resolution,
        "aspect": args.aspect,
    }
    if args.loop:
        # The sequence returns to its first frame, so the scrub has no seam.
        params["end_image"] = params["start_image"]

    run_generation(
        args.project, "video", out_name=args.out, cli_model=args.model,
        note=args.note or ("looping" if args.loop else ""),
        derived_from=still.stem, overwrite=args.overwrite, params=params,
    )
    return 0


def cmd_cutout(args: argparse.Namespace) -> int:
    directory = require_project(args.project)
    source = Path(args.image)
    for option in (source, directory / source, directory / "build" / source.name):
        if option.is_file():
            source = option
            break
    else:
        die(f"no image at {args.image}")

    out_name = args.out or f"{source.stem}_cutout.png"
    path = run_generation(
        args.project, "cutout", out_name=out_name, cli_model=args.model,
        note="alpha matte", derived_from=source.stem, overwrite=args.overwrite,
        params={"start_image": data_uri(source)},
    )

    # Publish into site/ as well: transparency rules out JPEG, so this stays PNG.
    published = directory / "site" / "cutout" / path.name
    published.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, published)
    say(f"  {mark('ok')} published {rel_posix(published, directory / 'site')}")
    return 0


CLIP_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".m4v")


def resolve_clip(directory: Path, name: str, given: "str | None") -> Path:
    """Locate the source clip. Defaults to build/<name>.<known video extension>."""
    if given:
        candidate = Path(given)
        for option in (candidate, directory / candidate, ROOT / candidate):
            if option.is_file():
                return option
        die(f"no clip at {given}")

    build = directory / "build"
    matches = [build / f"{name}{ext}" for ext in CLIP_EXTENSIONS
               if (build / f"{name}{ext}").is_file()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        present = sorted(p.name for p in build.glob("*") if p.is_file()) if build.exists() else []
        die(f"no clip for section '{name}' — expected {build / (name + '.mp4')}\n"
            f"  build/ contains: {', '.join(present) if present else 'nothing yet'}\n"
            f"  Pass --clip, or --placeholder to slice free frames with no clip at all.")
    die(f"several clips match '{name}': {', '.join(m.name for m in matches)} — pass --clip")


def cmd_frames(args: argparse.Namespace) -> int:
    directory = require_project(args.project)
    defaults = load_project_config(args.project).get("defaults") or DEFAULTS

    count = args.count if args.count is not None else defaults.get("count", 180)
    width = args.width if args.width is not None else defaults.get("width", 1600)
    mobile_width = (args.mobile_width if args.mobile_width is not None
                    else defaults.get("mobile_width", 900))
    fmt = args.format or defaults.get("format", "webp")
    quality = args.quality if args.quality is not None else defaults.get("quality", 80)

    ffmpeg, ffprobe = ensure_ffmpeg()
    site = directory / "site"

    if args.placeholder:
        duration = PLACEHOLDER_SECONDS
        height = round(width * 9 / 16)
        height += height % 2
        input_args = ["-f", "lavfi",
                      "-i", f"testsrc2=size={width}x{height}:rate=30:duration={duration:g}"]
        source_label = f"testsrc2 {width}x{height} {duration:g}s"
        clip_record = None
        say(f"{mark('dot')} placeholder — no clip, no API call, $0")
    else:
        clip = resolve_clip(directory, args.name, args.clip)
        duration = probe_duration(ffprobe, clip)
        input_args = ["-i", str(clip)]
        source_label = f"{clip.name} ({duration:.2f}s)"
        clip_record = rel_posix(clip, directory)

    say(f"  source    {source_label}")
    say(f"  format    {fmt} q{quality}")
    if args.trim_end:
        say(f"  trim-end  {args.trim_end:g}s (sampling the first {duration - args.trim_end:.2f}s)")

    plan = [("desktop", width, count)]
    if not args.desktop_only:
        # The spec's floor of 40 assumes the default 180. Below a count of 80 it
        # would hand the weaker device *more* frames than the desktop variant,
        # which is never intended, so it is capped at the desktop count.
        plan.append(("mobile", mobile_width, min(max(count // 2, 40), count)))

    variants: dict = {}
    for variant, variant_width, variant_count in plan:
        dest = site / "frames" / args.name / variant
        written = slice_frames(
            ffmpeg, input_args, dest,
            count=variant_count, width=variant_width, fmt=fmt, quality=quality,
            duration=duration, trim_end=args.trim_end,
        )
        if not written:
            die(f"no frames were written to {dest}")
        size_mb = sum(p.stat().st_size for p in dest.glob(f"{FRAME_GLOB}.{fmt}")) / 1e6
        note = "" if written == variant_count else f"  (asked for {variant_count})"
        say(f"  {mark('ok')} {variant:<8} {written} frames at {variant_width}px, "
            f"{size_mb:.1f} MB{note}")
        variants[variant] = {"count": written, "width": variant_width,
                             "bytes": int(size_mb * 1e6)}

    first_frame = site / "frames" / args.name / "desktop" / f"frame_0001.{fmt}"
    posters: "list[Path]" = []
    if first_frame.exists():
        posters = write_poster(ffmpeg, first_frame, site / "poster", args.name, width)
        say(f"  {mark('ok')} poster   {', '.join(p.name for p in posters)} (from frame 1)")
    else:
        warn(f"expected {first_frame.name} for the poster but it is missing")

    with mutate_state(args.project) as state:
        state.setdefault("sections", {})[args.name] = {
            "clip": clip_record,
            "placeholder": bool(args.placeholder),
            "format": fmt,
            "quality": quality,
            "trim_end": args.trim_end,
            "source_duration": round(duration, 3),
            "variants": variants,
            "poster": rel_posix(posters[0], site) if posters else None,
            "sliced_at": now_iso(),
        }
        sections = dict(state["sections"])

    rule("paste into site/index.html")
    say(scrub_sections_snippet(sections))
    say()
    say(f"{mark('dot')} counts above are files on disk, not the requested numbers —")
    say("  ffmpeg's fps filter rounds, and a wrong count makes the engine fetch")
    say("  frames that do not exist.")
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

        def end_headers(self) -> None:
            # This is a QA server. Phase 8 iterates on brand.css and index.html,
            # and a cached stylesheet makes a fix look like it did nothing —
            # measured during the shakedown, where an applied rule read back as
            # max-width:none until the cache was bypassed.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

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

    def add_paid(parser):
        parser.add_argument("--project", required=True)
        parser.add_argument("--model", help="catalogue key or literal model id")
        parser.add_argument("--note", help="recorded against the spend entry")
        parser.add_argument("--overwrite", action="store_true",
                            help="replace an existing output (re-rendering costs money)")

    image = sub.add_parser("image", help="generate a still into build/")
    add_paid(image)
    image.add_argument("--prompt", required=True)
    image.add_argument("--out", required=True, help="filename inside build/")
    image.add_argument("--aspect", default="16:9")
    image.add_argument("--resolution")
    image.set_defaults(func=cmd_image)

    video = sub.add_parser("video", help="animate a still into build/")
    add_paid(video)
    video.add_argument("--prompt", required=True)
    video.add_argument("--image", required=True, help="the approved still")
    video.add_argument("--out", required=True, help="filename inside build/")
    video.add_argument("--duration", type=float, default=6.0)
    video.add_argument("--resolution", default="720p")
    video.add_argument("--aspect", default="16:9")
    video.add_argument("--loop", action="store_true",
                       help="end on the first frame so the scrub has no seam")
    video.set_defaults(func=cmd_video)

    cutout = sub.add_parser("cutout", help="background removal to a PNG with alpha")
    add_paid(cutout)
    cutout.add_argument("--image", required=True)
    cutout.add_argument("--out", help="default: <image>_cutout.png")
    cutout.set_defaults(func=cmd_cutout)

    frames = sub.add_parser("frames", help="slice a clip into numbered frames")
    frames.add_argument("--project", required=True)
    frames.add_argument("--name", required=True, help="section name, e.g. hero")
    frames.add_argument("--clip", help="source video (default: build/<name>.mp4)")
    frames.add_argument("--count", type=int, help="desktop frame count (default 180)")
    frames.add_argument("--width", type=int, help="desktop width in px (default 1600)")
    frames.add_argument("--mobile-width", type=int, dest="mobile_width",
                        help="mobile width in px (default 900)")
    frames.add_argument("--format", choices=["webp", "jpg", "avif"],
                        help="frame format (default webp; avif is slow, opt in)")
    frames.add_argument("--quality", type=int, help="0-100, higher is better (default 80)")
    frames.add_argument("--trim-end", type=float, default=0.0, dest="trim_end",
                        help="drop this many seconds off the end, for loop seams")
    frames.add_argument("--desktop-only", action="store_true", dest="desktop_only",
                        help="skip the mobile variant")
    frames.add_argument("--placeholder", action="store_true",
                        help="generate free frames with no clip and no API call")
    frames.set_defaults(func=cmd_frames)

    phase = sub.add_parser("phase", help="show or record consultation phase and gate approvals")
    phase.add_argument("--project", required=True)
    phase.add_argument("--set", help=f"one of: {', '.join(PHASES)}")
    phase.add_argument("--approve", help="gate name, e.g. directions, copy, still")
    phase.add_argument("--choice", help="what was chosen at that gate")
    phase.add_argument("--note", help="why")
    phase.set_defaults(func=cmd_phase)

    contact = sub.add_parser("contact",
                             help="tile frames into one sheet so the clip can be looked at")
    contact.add_argument("--project", required=True)
    contact.add_argument("--name", required=True)
    contact.add_argument("--cells", type=int, default=12)
    contact.add_argument("--cols", type=int, default=4)
    contact.add_argument("--width", type=int, default=400, help="per cell")
    contact.set_defaults(func=cmd_contact)

    pluck = sub.add_parser("pluck",
                           help="copy frames into site/still/ as page imagery ($0)")
    pluck.add_argument("--project", required=True)
    pluck.add_argument("--name", required=True, help="section the frames came from")
    pluck.add_argument("--frames", required=True, help="comma-separated, e.g. 45,90")
    pluck.add_argument("--crop", help="x,y,w,h as fractions of the source")
    pluck.add_argument("--width", type=int, default=1600)
    pluck.add_argument("--format", choices=["webp", "jpg", "avif"])
    pluck.add_argument("--quality", type=int, default=82)
    pluck.set_defaults(func=cmd_pluck)

    check = sub.add_parser("check", help="free QA gate; exits non-zero on failure")
    check.add_argument("--project", required=True)
    check.set_defaults(func=cmd_check)

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
