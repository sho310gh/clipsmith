"""Edit planning for one short: silence trimming and zoom (peak) moments."""
import re
import subprocess

PAD = 0.18  # seconds of silence kept on each side of a removed pause


def detect_silences(src, start, dur, min_pause):
    """Real silences (no speech AND no game sound) in [start, start+dur], as (a, b) relative to start."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                        "-vn", "-af", f"silencedetect=noise=-38dB:d={min_pause}", "-f", "null", "-"],
                       capture_output=True, text=True)
    out, a = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            a = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and a is not None:
            out.append((a, float(m.group(1))))
            a = None
    if a is not None:  # silence runs to the end of the clip
        out.append((a, dur))
    return out


def keep_segments(silences, dur):
    """Complement of the (shrunk) silences: the parts of the clip to keep."""
    cuts = [(a + PAD, b - PAD) for a, b in silences if b - a > 2 * PAD + 0.2]
    segs, pos = [], 0.0
    for a, b in cuts:
        if a - pos > 0.3:
            segs.append((round(pos, 3), round(a, 3)))
        pos = b
    if dur - pos > 0.3 or not segs:
        segs.append((round(pos, 3), round(dur, 3)))
    return segs


def remap(t, segs):
    """Time in the original clip -> time in the trimmed clip."""
    acc = 0.0
    for a, b in segs:
        if t <= b:
            return acc + max(0.0, t - a)
        acc += b - a
    return acc


def loud_peaks(src, start, dur, limit=5):
    """Loudest moments (relative time, score 0-1): shouting, explosions, sudden reactions. [] if numpy is missing."""
    try:
        import numpy as np
    except ImportError:
        return []
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(src), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)
    win = 800  # 0.1 s
    n = len(x) // win
    if n < 20:
        return []
    rms = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
    sm = np.convolve(rms, np.ones(3) / 3, mode="same")
    med, top = float(np.median(sm)), float(sm.max())
    if top < 300 or top < 1.5 * med:
        return []
    peaks = []
    for i in np.argsort(-sm):
        t = i * 0.1
        if sm[i] < 1.4 * med or t < 1.5 or t > dur - 2.0:
            continue
        if all(abs(t - p[0]) >= 3.0 for p in peaks):
            peaks.append((round(float(t), 2), round(float(sm[i] / top), 2)))
        if len(peaks) >= limit:
            break
    return peaks


def zoom_moments(src, start, dur, ai_peaks, segs, limit=5):
    """Zoom candidates in trimmed-clip time as [[t, score]]: AI-picked moments first, then loudness peaks."""
    cands = [(float(p) - start, 1.0) for p in (ai_peaks or []) if 1.5 <= float(p) - start <= dur - 2.0]
    for t, sc in loud_peaks(src, start, dur):
        if all(abs(t - c[0]) >= 3.0 for c in cands):
            cands.append((t, sc * 0.9))
    out = []
    for t, sc in cands[: limit]:
        tt = remap(t, segs)
        if tt >= 1.2 and all(abs(tt - o[0]) >= 2.5 for o in out):
            out.append([round(tt, 2), round(sc, 2)])
    return sorted(out)
