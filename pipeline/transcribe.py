"""Step 2: speech -> text with word-level timestamps (faster-whisper, local)."""
import glob
import json
import os
import sys

from .media import Cancelled
from .project import safe_replace

CHECKPOINT_EVERY = 25  # segments
GPU_ERRORS = ("cublas", "cudnn", "cuda", "cufft", "curand", "cusparse")


def _add_nvidia_dll_dirs():
    """Windows: make CUDA DLLs installed via pip (requirements-gpu.txt) findable by CTranslate2."""
    if sys.platform != "win32":
        return
    import site
    for root in list(site.getsitepackages()) + [site.getusersitepackages()]:
        for d in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _transcribe(ctx, WhisperModel, device, compute, audio, segments, partial, duration, prefix):
    """One transcription attempt. Appends to `segments` (shared) so a retry can resume where this stopped."""
    p = ctx.project
    st = p.settings
    p.set_step("transcribe", "running", f"{prefix}Loading Whisper model '{st['whisper_model']}'", 0.1)
    model = WhisperModel(st["whisper_model"], device=device, compute_type=compute)

    lang = None if st["language"] in ("", "auto") else st["language"]
    kwargs = dict(word_timestamps=True, language=lang, vad_filter=True)
    if segments:  # resume after interruption or after a device switch
        kwargs["clip_timestamps"] = [segments[-1]["end"]]
    gen, info = model.transcribe(str(audio), **kwargs)

    since = 0
    for seg in gen:  # CUDA library errors usually surface here, on the first iteration
        ctx.check()
        words = [{"w": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)}
                 for w in (seg.words or []) if w.word.strip()]
        if not words:
            continue
        segments.append({"start": round(seg.start, 3), "end": round(seg.end, 3),
                         "text": seg.text.strip(), "words": words})
        since += 1
        p.set_step("transcribe", "running", f"{prefix}Transcribing ({int(seg.end // 60)} min done)",
                   0.1 + 0.9 * min(1.0, seg.end / duration))
        if since >= CHECKPOINT_EVERY:
            partial.write_text(json.dumps({"segments": segments}), encoding="utf-8")
            since = 0
    return info


def run(ctx):
    p = ctx.project
    out_dir = p.step_dir("transcribe")
    final = out_dir / "transcript.json"
    partial = out_dir / "transcript.partial.json"
    duration = p.data["source"]["duration"]

    if not p.data["source"].get("has_audio"):
        final.write_text(json.dumps({"language": "none", "duration": duration, "segments": []}), encoding="utf-8")
        p.set_step("transcribe", "running", "No audio track - empty transcript", 1)
        return

    audio = out_dir / "audio.wav"
    if not audio.is_file():
        p.set_step("transcribe", "running", "Extracting audio", 0)
        tmp = out_dir / "audio.part.wav"
        ctx.ffmpeg(["-i", str(p.source_path()), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)],
                   duration=duration, on_progress=lambda f: p.set_step("transcribe", "running", "Extracting audio", f * 0.1))
        safe_replace(tmp, audio)

    _add_nvidia_dll_dirs()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("faster-whisper is not installed. Run: pip install -r requirements.txt")

    segments = []
    if partial.is_file():
        segments = json.loads(partial.read_text(encoding="utf-8"))["segments"]

    # GPU if it works, otherwise CPU (set WHISPER_DEVICE=cpu to skip the GPU attempt).
    plan = [("cpu", "int8", "CPU: ")] if os.environ.get("WHISPER_DEVICE") == "cpu" else [("auto", "auto", ""), ("cpu", "int8", "CPU: ")]
    info = None
    for i, (device, compute, prefix) in enumerate(plan):
        try:
            info = _transcribe(ctx, WhisperModel, device, compute, audio, segments, partial, duration, prefix)
            break
        except Cancelled:
            raise
        except Exception as e:
            if i + 1 < len(plan) and any(k in str(e).lower() for k in GPU_ERRORS):
                p.set_step("transcribe", "running", "GPU libraries missing - switching to CPU (slower)", 0.1)
                continue
            raise

    final.write_text(json.dumps({"language": info.language, "duration": duration, "segments": segments},
                                ensure_ascii=False), encoding="utf-8")
    if partial.is_file():
        partial.unlink()
    audio.unlink(missing_ok=True)  # keep the folder small; re-extracted if ever needed
    p.set_step("transcribe", "running", f"{len(segments)} segments, language {info.language}", 1)


def load(project):
    f = project.dir / "02_transcript" / "transcript.json"
    return json.loads(f.read_text(encoding="utf-8"))
