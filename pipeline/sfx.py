"""Sound effects. Generated with ffmpeg on first use (no licensing issues).
Replace sfx/whoosh.wav or sfx/pop.wav with your own sounds any time (wav/mp3/ogg)."""
import subprocess

from .project import ROOT

SFX_DIR = ROOT / "sfx"
EXTS = (".wav", ".mp3", ".ogg", ".m4a", ".flac")

GENERATORS = {
    # filtered pink noise with a flanger sweep and a swell/decay envelope
    "whoosh": ["-f", "lavfi", "-i", "anoisesrc=color=pink:duration=0.8:sample_rate=48000:amplitude=0.8", "-af",
               "highpass=f=400,lowpass=f=7000,flanger=delay=8:depth=9:speed=3,"
               "afade=t=in:st=0:d=0.5:curve=qsin,afade=t=out:st=0.5:d=0.3:curve=exp,aformat=channel_layouts=stereo"],
    # pitch-dropping thud for punch-in zooms
    "pop": ["-f", "lavfi", "-i", "aevalsrc='sin(2*PI*(120-250*t)*t)*exp(-12*t)':d=0.3:s=48000", "-af",
            "aformat=channel_layouts=stereo"],
}


def get(name):
    """Path of the sound `name` ('whoosh' or 'pop'); a user file wins, otherwise it is generated once."""
    SFX_DIR.mkdir(exist_ok=True)
    for ext in EXTS:
        f = SFX_DIR / f"{name}{ext}"
        if f.is_file() and f.stat().st_size > 0:
            return f
    out = SFX_DIR / f"{name}.wav"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *GENERATORS[name], str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Could not generate {name} sound: {r.stderr[-400:]}")
    return out
