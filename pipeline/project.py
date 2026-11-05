"""Project state: folder layout, project.json, step bookkeeping."""
import json
import os
import re
import shutil
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"
FONTS_DIR = ROOT / "fonts"
PREFS_FILE = ROOT / "user_prefs.json"   # API key + last-used settings (local, git-ignored)

STEPS = ["ingest", "transcribe", "analyze", "cut", "subtitles", "render"]
STEP_DIRS = {
    "ingest": "01_source",
    "transcribe": "02_transcript",
    "analyze": "03_analysis",
    "cut": "04_cuts",
    "subtitles": "05_subtitles",
    "render": "06_shorts",
}

DEFAULT_STYLE = {
    "font": "Arial",
    "size": 78,
    "bold": True,
    "uppercase": True,
    "primary": "#FFFFFF",
    "highlight": "#FFD400",
    "highlight_words": True,
    "words_per_line": 3,
    "outline": 6,
    "outline_color": "#000000",
    "shadow": 4,
    "shadow_color": "#000000",
    "shadow_opacity": 70,
    "y_percent": 68,
}

DEFAULT_SETTINGS = {
    "count": 10,
    "min_len": 30,
    "max_len": 60,
    "format": "fit",          # "fit" (blurred zoomed bg) or "fill" (height filled)
    "crop_x": 50,             # fill only: horizontal crop position 0-100
    "whisper_model": "small",
    "language": "auto",
    "gemini_model": "",
    "style": DEFAULT_STYLE,
    "effects": {
        "intro": {"enabled": True, "style": "zoom", "duration": 0.6, "whoosh": True, "whoosh_volume": 55},
        "hook": {"enabled": True, "duration": 2.5, "size": 96, "color": "#FFD400", "y_percent": 20},
        "zoom": {"enabled": True, "strength": 10, "max_per_short": 3, "sfx": True},
        "loop": {"enabled": False, "fade": 0.4},
        "audio": {"normalize": True},
        "trim": {"enabled": False, "min_pause": 0.8},
        "metadata": {"enabled": True},
    },
}

# Which step must be redone when a setting changes (None = nothing to redo).
SETTING_STEP = {
    "whisper_model": "transcribe",
    "language": "transcribe",
    "count": "analyze",
    "min_len": "analyze",
    "max_len": "analyze",
    "gemini_model": "analyze",
    "style": "subtitles",
    "format": "render",
    "crop_x": "render",
    "effects.intro": "render",
    "effects.zoom": "render",
    "effects.loop": "render",
    "effects.audio": "render",
    "effects.hook": "subtitles",
    "effects.trim": "cut",
    "effects.metadata": None,
}


def setting_diffs(old, new):
    """Keys of SETTING_STEP whose values differ between two settings dicts."""
    out = []
    for k in SETTING_STEP:
        a, b = old, new
        for part in k.split("."):
            a, b = (a or {}).get(part), (b or {}).get(part)
        if a != b:
            out.append(k)
    return out


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def slug(text):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:40] or "project"


def short_name(index):
    return f"short_{index:02d}"


def safe_replace(src, dst):
    """os.replace that survives Windows file locks (Google Drive sync, antivirus, indexers)."""
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    shutil.copyfile(src, dst)  # last resort: overwrite the content in place
    os.remove(src)


class Project:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.lock = threading.RLock()
        self.data = {}

    # ---- creation / loading -------------------------------------------
    @classmethod
    def create(cls, name, settings=None):
        PROJECTS_DIR.mkdir(exist_ok=True)
        pid = f"{slug(name)}-{uuid.uuid4().hex[:6]}"
        p = cls(PROJECTS_DIR / pid)
        p.dir.mkdir(parents=True)
        merged = deepcopy(DEFAULT_SETTINGS)
        merge_settings(merged, load_prefs().get("settings", {}))  # start from last-used settings
        merge_settings(merged, settings or {})
        p.data = {
            "version": 1,
            "id": pid,
            "name": name,
            "created": now(),
            "updated": now(),
            "state": "created",
            "settings": merged,
            "source": {},
            "steps": {s: {"status": "pending", "message": "", "progress": 0} for s in STEPS},
            "shorts": [],
        }
        p.save()
        return p

    @classmethod
    def open(cls, pid):
        folder = PROJECTS_DIR / pid
        f = folder / "project.json"
        if not f.is_file():
            raise FileNotFoundError(f"No project '{pid}' in {PROJECTS_DIR}")
        p = cls(folder)
        p.data = json.loads(f.read_text(encoding="utf-8"))
        base = deepcopy(DEFAULT_SETTINGS)  # older projects get defaults for newer settings
        merge_settings(base, p.data.get("settings", {}))
        p.data["settings"] = base
        return p

    def recover(self):
        """Called when no worker is alive: a 'running' state means a crash/kill."""
        with self.lock:
            if self.data["state"] == "running":
                self.data["state"] = "interrupted"
            for s, info in self.data["steps"].items():
                if info["status"] == "running":
                    info["status"] = "pending"
                    info["message"] = "Interrupted - resume to continue"
            self.save()

    # ---- persistence ---------------------------------------------------
    def save(self):
        with self.lock:
            self.data["updated"] = now()
            tmp = self.dir / "project.json.tmp"
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            safe_replace(tmp, self.dir / "project.json")

    # ---- helpers -------------------------------------------------------
    @property
    def settings(self):
        return self.data["settings"]

    def step_dir(self, step):
        d = self.dir / STEP_DIRS[step]
        d.mkdir(exist_ok=True)
        return d

    def source_path(self):
        src = self.data.get("source", {})
        if not src:
            raise RuntimeError("No source video yet")
        path = Path(src["path"])
        return path if path.is_absolute() else self.dir / path

    def step_status(self, step):
        return self.data["steps"][step]["status"]

    def set_state(self, state):
        with self.lock:
            self.data["state"] = state
            self.save()

    def set_step(self, step, status, message=None, progress=None):
        with self.lock:
            info = self.data["steps"][step]
            info["status"] = status
            if message is not None:
                info["message"] = message
            if progress is not None:
                info["progress"] = round(float(progress), 3)
            self.save()

    def set_short(self, index, **fields):
        with self.lock:
            for s in self.data["shorts"]:
                if s["index"] == index:
                    s.update(fields)
            self.save()

    def reset_from(self, step):
        """Mark `step` and everything after it pending and delete their outputs."""
        with self.lock:
            start = STEPS.index(step)
            for s in STEPS[start:]:
                if s == "ingest":
                    continue
                self.data["steps"][s] = {"status": "pending", "message": "", "progress": 0}
                d = self.dir / STEP_DIRS[s]
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
            if STEPS.index(step) <= STEPS.index("analyze"):
                self.data["shorts"] = []
            else:
                reset = {"cut": ("cut", "subtitles", "render"),
                         "subtitles": ("subtitles", "render"),
                         "render": ("render",)}[step]
                for sh in self.data["shorts"]:
                    for k in reset:
                        sh[k] = "pending"
            if self.data["state"] in ("done", "error", "cancelled", "interrupted"):
                self.data["state"] = "created"
            self.save()


def merge_settings(base, new):
    """Merge user settings into base, ignoring unknown keys."""
    for k, v in (new or {}).items():
        if k not in base:
            continue
        if isinstance(base[k], dict) and isinstance(v, dict):
            merge_settings(base[k], v)
        else:
            base[k] = v
    return base


def load_prefs():
    try:
        return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_prefs(**fields):
    prefs = load_prefs()
    prefs.update(fields)
    tmp = PREFS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
    safe_replace(tmp, PREFS_FILE)


def list_projects():
    out = []
    if not PROJECTS_DIR.is_dir():
        return out
    for d in sorted(PROJECTS_DIR.iterdir()):
        f = d / "project.json"
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                out.append({"id": data["id"], "name": data["name"], "state": data["state"],
                            "updated": data.get("updated", ""), "shorts": len(data.get("shorts", []))})
            except Exception:
                pass
    return out
