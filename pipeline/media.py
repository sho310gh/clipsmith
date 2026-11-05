"""ffmpeg/ffprobe helpers and the cancellable runner context."""
import json
import subprocess
import tempfile
import threading


class Cancelled(Exception):
    pass


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[-500:]}")
    info = json.loads(out.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise RuntimeError("File has no video stream")
    fps = 30.0
    try:
        n, d = video.get("avg_frame_rate", "30/1").split("/")
        fps = float(n) / float(d) if float(d) else 30.0
    except Exception:
        pass
    return {
        "duration": float(info["format"].get("duration") or video.get("duration") or 0),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": round(fps, 3),
        "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
    }


class Context:
    """Passed to every pipeline step. Holds the project, API key and cancel flag."""

    def __init__(self, project, api_key=""):
        self.project = project
        self.api_key = api_key
        self.cancel = threading.Event()
        self._proc = None

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def stop(self):
        self.cancel.set()
        p = self._proc
        if p is not None and p.poll() is None:
            p.terminate()

    def ffmpeg(self, args, duration=None, on_progress=None, cwd=None):
        """Run ffmpeg; report progress 0-1 via on_progress; raise on failure/cancel."""
        self.check()
        cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"] + args
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True, cwd=cwd)
            self._proc = proc
            try:
                for line in proc.stdout:
                    if self.cancel.is_set():
                        proc.terminate()
                        break
                    if duration and on_progress and line.startswith("out_time_us="):
                        try:
                            on_progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
                        except ValueError:
                            pass
                proc.wait()
            finally:
                self._proc = None
            if self.cancel.is_set():
                raise Cancelled()
            if proc.returncode != 0:
                err.seek(0)
                raise RuntimeError("ffmpeg failed: " + err.read().decode(errors="replace")[-1500:])
