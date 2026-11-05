"""Steps 4 and 6: cut each short from the source (optionally trimming silences), then lay it out
(fill/fit), apply effects (intro, zoom punches, loop fade, loudness) and burn subtitles."""
import subprocess
import tempfile
from pathlib import Path

from . import edit, sfx
from .project import FONTS_DIR, safe_replace, short_name
from .subtitles import build_ass, sample_words

W, H = 1080, 1920
CODEC_V = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]


def ass_filter(ass_name):
    f = f"ass=filename={ass_name}"
    if FONTS_DIR.is_dir() and any(p.suffix.lower() in (".ttf", ".otf", ".ttc") for p in FONTS_DIR.iterdir()):
        d = str(FONTS_DIR).replace("\\", "/").replace(":", "\\:")
        f += f":fontsdir='{d}'"
    return f


def build_filter(fmt, crop_x, post):
    """Return a filter_complex video part ending in [v]. `post` is the chain applied after layout."""
    if fmt == "fill":
        cx = max(0, min(100, float(crop_x))) / 100
        return (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H}:(iw-{W})*{cx}:(ih-{H})/2,setsar=1[v0];[v0]{post}[v]")
    # fit: sharp video at full width on top of a zoomed, blurred copy of itself
    return (f"[0:v]split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=6,"
            f"scale={W}:{H},eq=brightness=-0.08[bg];"
            f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0];[v0]{post}[v]")


# ---------------------------------------------------------------- cut
def run_cut(ctx):
    p = ctx.project
    fx = p.settings["effects"]
    out_dir = p.step_dir("cut")
    src = str(p.source_path())
    has_audio = p.data["source"].get("has_audio", True)
    shorts = p.data["shorts"]
    total = len(shorts)
    for n, s in enumerate(shorts):
        ctx.check()
        name = short_name(s["index"])
        out = out_dir / f"{name}.mp4"
        dur = s["end"] - s["start"]
        if out.is_file() and s.get("plan"):
            p.set_short(s["index"], cut="done")
            continue
        p.set_step("cut", "running", f"Analyzing {n + 1}/{total}", n / total)
        if out.is_file():  # clip from an older version: untrimmed
            segs = [(0.0, round(dur, 3))]
        elif fx["trim"]["enabled"] and has_audio:
            segs = edit.keep_segments(edit.detect_silences(src, s["start"], dur, fx["trim"]["min_pause"]), dur)
        else:
            segs = [(0.0, round(dur, 3))]
        out_dur = round(sum(b - a for a, b in segs), 3)
        try:
            zooms = edit.zoom_moments(src, s["start"], dur, s.get("peaks"), segs)
        except Exception:
            zooms = []
        plan = {"segments": segs, "out_dur": out_dur, "zooms": zooms}
        p.set_short(s["index"], plan=plan)
        if out.is_file():
            p.set_short(s["index"], cut="done")
            continue

        tmp = out_dir / f"{name}.part.mp4"
        pre = ["-ss", f"{s['start']:.3f}", "-t", f"{dur:.3f}", "-i", src]
        if len(segs) == 1 and segs[0][0] == 0 and abs(segs[0][1] - dur) < 0.01:
            args = pre + CODEC_V + ["-crf", "16"]
        else:  # concatenate the kept parts
            parts, labels = [], ""
            for i, (a, b) in enumerate(segs):
                parts.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]")
                if has_audio:
                    parts.append(f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}]")
                labels += f"[v{i}]" + (f"[a{i}]" if has_audio else "")
            parts.append(f"{labels}concat=n={len(segs)}:v=1:a={1 if has_audio else 0}[v]" + ("[a]" if has_audio else ""))
            args = pre + ["-filter_complex", ";".join(parts), "-map", "[v]"] + (["-map", "[a]"] if has_audio else []) \
                + CODEC_V + ["-crf", "16"]
        ctx.ffmpeg(args + ["-c:a", "aac", "-b:a", "192k", str(tmp)], duration=out_dur,
                   on_progress=lambda f, i=n: p.set_step("cut", "running", f"Cutting {i + 1}/{total}", (i + f) / total))
        safe_replace(tmp, out)
        p.set_short(s["index"], cut="done")
        p.set_step("cut", "running", f"Cutting {n + 1}/{total}", (n + 1) / total)


# ---------------------------------------------------------------- render
def build_render(settings, plan, ass_name, has_audio):
    """Return (extra input files, filter_complex string) for one short."""
    fx = settings["effects"]
    D = float(plan["out_dur"])
    intro, zoom, loop = fx["intro"], fx["zoom"], fx["loop"]

    # video: zoom terms (intro settle + punch-ins), subtitles, fades
    terms, zooms = [], []
    if intro["enabled"] and intro["style"] == "zoom":
        terms.append(f"0.12*max(0,1-t/{max(0.2, float(intro['duration']))})")
    if zoom["enabled"] and plan.get("zooms"):
        k = float(zoom["strength"]) / 100
        best = sorted(plan["zooms"], key=lambda z: -z[1])[: int(zoom["max_per_short"])]
        zooms = sorted(z[0] for z in best)
        for tk in zooms:
            terms.append(f"{k}*clip((t-{tk})/0.12,0,1)*clip(1-(t-{tk}-0.12)/0.9,0,1)")
    post = []
    if terms:
        f = "(" + "+".join(terms) + ")"
        post.append(f"scale=w='trunc({W}*(1+{f})/2)*2':h='trunc({H}*(1+{f})/2)*2':eval=frame,crop={W}:{H}")
    post.append(ass_filter(ass_name))
    if intro["enabled"]:
        d = max(0.2, float(intro["duration"]))
        if intro["style"] == "flash":
            post.append("fade=t=in:st=0:d=0.3:color=white")
        else:
            post.append(f"fade=t=in:st=0:d={d if intro['style'] == 'fade' else min(d, 0.35)}")
    if loop["enabled"]:
        L = min(float(loop["fade"]), D / 3)
        post.append(f"fade=t=out:st={max(0, D - L):.3f}:d={L:.3f}")
    graph = build_filter(settings["format"], settings["crop_x"], ",".join(post))

    # audio: loudness, whoosh at the start, pops on zoom punches, loop fade
    inputs = []
    if has_audio:
        chain = (["loudnorm=I=-14:TP=-1.5:LRA=11"] if fx["audio"]["normalize"] else []) + \
                ["aresample=48000", "aformat=channel_layouts=stereo"]
        parts, mix, idx = [f"[0:a]{','.join(chain)}[m]"], ["[m]"], 1
        norm = "aresample=48000,aformat=channel_layouts=stereo"
        if intro["enabled"] and intro["whoosh"]:
            inputs.append(sfx.get("whoosh"))
            parts.append(f"[{idx}:a]{norm},volume={float(intro['whoosh_volume']) / 100:.2f}[w]")
            mix.append("[w]")
            idx += 1
        if zooms and zoom["sfx"]:
            inputs.append(sfx.get("pop"))
            n = len(zooms)
            ms = [int(t * 1000) for t in zooms]
            if n == 1:
                parts.append(f"[{idx}:a]{norm},volume=0.5,adelay={ms[0]}|{ms[0]}[p0]")
            else:
                parts.append(f"[{idx}:a]{norm},volume=0.5,asplit={n}" + "".join(f"[q{i}]" for i in range(n)))
                parts += [f"[q{i}]adelay={m}|{m}[p{i}]" for i, m in enumerate(ms)]
            mix += [f"[p{i}]" for i in range(n)]
        if len(mix) > 1:
            parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[a0]")
        else:
            parts.append("[m]anull[a0]")
        if loop["enabled"]:
            L = min(float(loop["fade"]), D / 3)
            parts.append(f"[a0]afade=t=out:st={max(0, D - L):.3f}:d={L:.3f}[a]")
        else:
            parts.append("[a0]anull[a]")
        graph += ";" + ";".join(parts)
    return inputs, graph


def run_render(ctx):
    p = ctx.project
    cut_dir, sub_dir, out_dir = p.step_dir("cut"), p.step_dir("subtitles"), p.step_dir("render")
    has_audio = p.data["source"].get("has_audio", True)
    shorts = p.data["shorts"]
    total = len(shorts)
    for n, s in enumerate(shorts):
        name = short_name(s["index"])
        out = out_dir / f"{name}.mp4"
        if out.is_file():
            p.set_short(s["index"], render="done")
            continue
        tmp = out_dir / f"{name}.part.mp4"
        dur = s["end"] - s["start"]
        plan = s.get("plan") or {"segments": [[0, dur]], "out_dur": dur, "zooms": []}
        inputs, graph = build_render(p.settings, plan, f"{name}.ass", has_audio)
        args = ["-i", str((cut_dir / f"{name}.mp4").resolve())]
        for f in inputs:
            args += ["-i", str(Path(f).resolve())]
        args += ["-filter_complex", graph, "-map", "[v]"] + (["-map", "[a]"] if has_audio else []) + \
            CODEC_V + ["-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp.resolve())]
        ctx.ffmpeg(args, duration=plan["out_dur"], cwd=str(sub_dir),
                   on_progress=lambda f, i=n: p.set_step("render", "running", f"Rendering {i + 1}/{total}", (i + f) / total))
        safe_replace(tmp, out)
        p.set_short(s["index"], render="done")
        p.set_step("render", "running", f"Rendering {n + 1}/{total}", (n + 1) / total)
    write_metadata(p)


def write_metadata(p):
    """Upload text (title, description, hashtags) next to each finished short."""
    if not p.settings["effects"]["metadata"]["enabled"]:
        return
    out_dir = p.step_dir("render")
    for s in p.data["shorts"]:
        if s.get("render") != "done":
            continue
        tags = " ".join(t if t.startswith("#") else f"#{t}" for t in (s.get("hashtags") or []))
        text = f"TITLE\n{s['title']}\n\nDESCRIPTION\n{s.get('description') or s.get('reason', '')}\n\nHASHTAGS\n{tags}\n"
        (out_dir / f"{short_name(s['index'])}.txt").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- preview
def preview_png(project, style, fmt, crop_x, hook=None):
    """One 540x960 frame with sample subtitles (and hook text) through the same layout and libass as the real render."""
    src = project.source_path()
    t = project.data["source"]["duration"] * 0.3
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        (tmpd / "sample.ass").write_text(build_ass(sample_words(), style, hook), encoding="utf-8")
        graph = build_filter(fmt, crop_x, f"setpts=PTS+0.8/TB,{ass_filter('sample.ass')},scale=540:960")
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(src),
                            "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-y", "preview.png"],
                           cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0 or not (tmpd / "preview.png").is_file():
            raise RuntimeError("Preview failed: " + r.stderr[-800:])
        return (tmpd / "preview.png").read_bytes()
