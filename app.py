"""ClipSmith - local web server. Run: python app.py  ->  http://127.0.0.1:5000"""
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from pipeline import analyze, render
from pipeline.media import Context, probe
from pipeline.project import (DEFAULT_SETTINGS, FONTS_DIR, PROJECTS_DIR, SETTING_STEP, STEPS, STEP_DIRS,
                              Project, list_projects, load_prefs, merge_settings, save_prefs, setting_diffs)
from pipeline.runner import run_pipeline

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)

_projects = {}   # id -> Project (shared lock/state)
_workers = {}    # id -> (thread, Context)
_reg_lock = threading.Lock()


def get_project(pid):
    with _reg_lock:
        if pid not in _projects:
            try:
                p = Project.open(pid)
            except FileNotFoundError:
                abort(404, f"Unknown project {pid}")
            if not is_running(pid):
                p.recover()
            _projects[pid] = p
        return _projects[pid]


def is_running(pid):
    w = _workers.get(pid)
    return bool(w and w[0].is_alive())


def err(msg, code=400):
    return jsonify({"error": msg}), code


# ---- static ---------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(ROOT / "web", "index.html")


@app.get("/files/<pid>/<path:rel>")
def files(pid, rel):
    get_project(pid)
    return send_from_directory(PROJECTS_DIR / pid, rel, conditional=True)


# ---- config / fonts -------------------------------------------------------
_font_cache = []


def font_families():
    if _font_cache:
        return _font_cache
    names = set()
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    n = winreg.EnumValue(k, i)[0].split(" (")[0]
                    for suf in (" Bold Italic", " Bold", " Italic", " Regular"):
                        n = n.removesuffix(suf)
                    names.add(n)
        else:
            out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                names.add(line.split(",")[0].strip())
    except Exception:
        pass
    try:  # fonts dropped into ./fonts
        from fontTools.ttLib import TTFont
        for f in FONTS_DIR.glob("*"):
            if f.suffix.lower() in (".ttf", ".otf"):
                names.add(TTFont(str(f), lazy=True)["name"].getBestFamilyName())
    except Exception:
        pass
    names.discard("")
    _font_cache.extend(sorted(names) or ["Arial", "Impact", "Verdana"])
    return _font_cache


@app.get("/api/config")
def config():
    return jsonify({"defaults": DEFAULT_SETTINGS, "steps": STEPS, "fonts": font_families()})


@app.get("/api/prefs")
def get_prefs():
    return jsonify({"api_key": load_prefs().get("api_key", "")})


@app.post("/api/prefs")
def set_prefs():
    """Save the Gemini API key locally (plain text in user_prefs.json); empty string forgets it."""
    save_prefs(api_key=((request.json or {}).get("api_key") or "").strip())
    return jsonify({"ok": True})


@app.post("/api/models")
def models():
    key = ((request.json or {}).get("api_key") or load_prefs().get("api_key", "")).strip()
    if not key:
        return err("API key required")
    try:
        return jsonify({"models": analyze.list_models(key)})
    except Exception as e:
        return err(f"Could not list models: {str(e)[:300]}")


# ---- projects -------------------------------------------------------------
@app.get("/api/projects")
def projects():
    return jsonify(list_projects())


@app.post("/api/projects")
def create_project():
    """multipart: name, (file | path), optional settings JSON."""
    name = (request.form.get("name") or "").strip() or "My project"
    link = (request.form.get("path") or "").strip().strip('"')
    up = request.files.get("file")
    if not up and not link:
        return err("Choose a video file or enter a path")
    if link and not Path(link).is_file():
        return err(f"File not found: {link}")
    p = Project.create(name, json.loads(request.form.get("settings") or "{}"))
    try:
        if up:
            ext = Path(up.filename or "video.mp4").suffix or ".mp4"
            dest = p.step_dir("ingest") / f"source{ext}"
            up.save(dest)
            p.data["source"] = {"mode": "upload", "path": f"{STEP_DIRS['ingest']}/{dest.name}",
                                "original_name": up.filename}
        else:
            p.data["source"] = {"mode": "link", "path": str(Path(link).resolve()), "original_name": Path(link).name}
        p.data["source"].update(probe(p.source_path()))
        p.set_step("ingest", "done", "Source ready", 1)
    except Exception as e:
        shutil.rmtree(p.dir, ignore_errors=True)
        return err(str(e))
    _projects[p.data["id"]] = p
    return jsonify(p.data)


@app.post("/api/projects/open")
def open_project_file():
    """Resume from an uploaded project.json (matched to its folder in ./projects)."""
    up = request.files.get("file")
    if not up:
        return err("No file")
    try:
        pid = json.loads(up.read().decode("utf-8"))["id"]
    except Exception:
        return err("Not a valid project.json")
    if not (PROJECTS_DIR / pid / "project.json").is_file():
        return err(f"Project folder 'projects/{pid}' not found next to the tool")
    return jsonify(get_project(pid).data)


@app.get("/api/projects/<pid>")
def project_info(pid):
    p = get_project(pid)
    return jsonify({**p.data, "running": is_running(pid)})


@app.patch("/api/projects/<pid>/settings")
def update_settings(pid):
    p = get_project(pid)
    if is_running(pid):
        return err("Project is running - cancel it first", 409)
    new = request.json or {}
    with p.lock:
        old = json.loads(json.dumps(p.settings))
        merge_settings(p.settings, new)
        changed = [k for k in setting_diffs(old, p.settings) if SETTING_STEP[k]]
        reset = None
        if changed:
            reset = min((SETTING_STEP[k] for k in changed), key=STEPS.index)
            # only redo a step if it had already produced something
            if p.step_status(reset) == "pending" and not any(
                    p.step_status(s) != "pending" for s in STEPS[STEPS.index(reset):] if s != "ingest"):
                reset = None
            else:
                p.reset_from(reset)
        p.save()
        save_prefs(settings=p.settings)  # new projects start from the last-used settings
    return jsonify({"reset_from": reset, "project": p.data})


@app.post("/api/projects/<pid>/start")
def start(pid):
    p = get_project(pid)
    if is_running(pid):
        return err("Already running", 409)
    api_key = ((request.json or {}).get("api_key") or load_prefs().get("api_key", "")).strip()
    ctx = Context(p, api_key)
    t = threading.Thread(target=run_pipeline, args=(ctx,), daemon=True)
    _workers[pid] = (t, ctx)
    t.start()
    return jsonify({"ok": True})


@app.post("/api/projects/<pid>/cancel")
def cancel(pid):
    w = _workers.get(pid)
    if w and w[0].is_alive():
        w[1].stop()
    return jsonify({"ok": True})


@app.post("/api/projects/<pid>/preview")
def preview(pid):
    p = get_project(pid)
    body = request.json or {}
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    merge_settings(merged, {"style": body.get("style", {}), "format": body.get("format"), "crop_x": body.get("crop_x", 50)})
    merge_settings(merged, {"effects": {"hook": body.get("hook") or {}}})
    fmt = body.get("format") if body.get("format") in ("fit", "fill") else p.settings["format"]
    hk = merged["effects"]["hook"]
    hook = {"text": "WAIT FOR IT", "duration": 5, "size": hk["size"], "color": hk["color"],
            "y": hk["y_percent"]} if hk["enabled"] else None
    try:
        png = render.preview_png(p, merged["style"], fmt, merged["crop_x"], hook)
    except Exception as e:
        return err(str(e), 500)
    return Response(png, mimetype="image/png")


if __name__ == "__main__":
    PROJECTS_DIR.mkdir(exist_ok=True)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"ClipSmith running at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True)
