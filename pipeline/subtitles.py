"""Step 5: build styled ASS subtitles (and a plain SRT) for each short from the transcript words."""
from .edit import remap
from .project import short_name
from .transcribe import load as load_transcript

W, H = 1080, 1920


def ass_color(hex_color, alpha=0):
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _t(sec):  # ASS time h:mm:ss.cc
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _srt_t(sec):
    ms = int(round(max(0.0, sec) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def words_in_range(transcript, start, end):
    out = []
    for seg in transcript["segments"]:
        if seg["end"] < start or seg["start"] > end:
            continue
        for w in seg["words"]:
            if w["start"] >= start - 0.05 and w["end"] <= end + 0.05:
                out.append({"w": w["w"], "start": max(0.0, w["start"] - start), "end": min(end - start, w["end"] - start)})
    return out


def group_lines(words, per_line):
    """Split words into display lines: max N words, break after a pause or sentence end."""
    lines, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        pause = i + 1 < len(words) and words[i + 1]["start"] - w["end"] > 0.7
        sentence = w["w"].endswith((".", "!", "?"))
        if len(cur) >= per_line or pause or sentence:
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return lines


def _esc(text, upper):
    text = text.replace("{", "(").replace("}", ")").replace("\\", "")
    return text.upper() if upper else text


def build_ass(words, style, hook=None):
    primary = ass_color(style["primary"])
    hl = ass_color(style["highlight"])
    back = ass_color(style["shadow_color"], int(round(255 * (1 - style["shadow_opacity"] / 100))))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style['font']},{int(style['size'])},{primary},{primary},{ass_color(style['outline_color'])},{back},{-1 if style['bold'] else 0},0,0,0,100,100,0,0,1,{style['outline']},{style['shadow']},5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    y = int(H * style["y_percent"] / 100)
    pos = f"{{\\an5\\pos({W // 2},{y})}}"
    up = style["uppercase"]
    lines = group_lines(words, max(1, int(style["words_per_line"])))
    events = []
    for li, line in enumerate(lines):
        nxt = lines[li + 1][0]["start"] if li + 1 < len(lines) else None
        line_end = line[-1]["end"] + 0.25
        if nxt is not None:
            line_end = min(line_end, nxt)
        if not style["highlight_words"]:
            text = " ".join(_esc(w["w"], up) for w in line)
            events.append(f"Dialogue: 0,{_t(line[0]['start'])},{_t(line_end)},Default,,0,0,0,,{pos}{text}")
            continue
        for wi, active in enumerate(line):
            start = active["start"] if wi else line[0]["start"]
            end = line[wi + 1]["start"] if wi + 1 < len(line) else line_end
            if end <= start:
                continue
            parts = []
            for j, w in enumerate(line):
                t = _esc(w["w"], up)
                parts.append(f"{{\\1c{hl}\\fscx108\\fscy108}}{t}{{\\1c{primary}\\fscx100\\fscy100}}" if j == wi else t)
            events.append(f"Dialogue: 0,{_t(start)},{_t(end)},Default,,0,0,0,,{pos}{' '.join(parts)}")
    if hook and hook.get("text"):  # bold title card that pops in at the start
        hy = int(H * hook["y"] / 100)
        tags = (f"{{\\an5\\pos({W // 2},{hy})\\fs{int(hook['size'])}\\b1\\1c{ass_color(hook['color'])}\\3c&H000000&"
                f"\\bord8\\fad(0,250)\\fscx60\\fscy60\\t(0,180,\\fscx100\\fscy100)}}")
        events.append(f"Dialogue: 1,{_t(0)},{_t(float(hook['duration']))},Default,,0,0,0,,{tags}{_esc(hook['text'], True)}")
    return header + "\n".join(events) + "\n"


def build_srt(words, style):
    out = []
    for i, line in enumerate(group_lines(words, max(1, int(style["words_per_line"]))), 1):
        text = " ".join(_esc(w["w"], style["uppercase"]) for w in line)
        out.append(f"{i}\n{_srt_t(line[0]['start'])} --> {_srt_t(line[-1]['end'])}\n{text}\n")
    return "\n".join(out)


def sample_words():
    """Fake words for the style preview; the preview frame is rendered at t=0.8s (2nd word active)."""
    text = "THIS IS HOW YOUR SUBTITLES WILL LOOK".split()
    return [{"w": w, "start": i * 0.5, "end": i * 0.5 + 0.5} for i, w in enumerate(text)]


def run(ctx):
    p = ctx.project
    tr = load_transcript(p)
    out_dir = p.step_dir("subtitles")
    shorts = p.data["shorts"]
    for n, s in enumerate(shorts):
        ctx.check()
        name = short_name(s["index"])
        words = words_in_range(tr, s["start"], s["end"])
        segs = (s.get("plan") or {}).get("segments") or []
        if len(segs) > 1:  # silences were trimmed: move the words onto the shortened timeline
            moved = [{"w": w["w"], "start": remap(w["start"], segs), "end": remap(w["end"], segs)} for w in words]
            words = [w for w in moved if w["end"] - w["start"] > 0.01]
        hk = p.settings["effects"]["hook"]
        hook = None
        if hk["enabled"]:
            hook = {"text": s.get("hook") or " ".join(s["title"].split()[:5]), "duration": hk["duration"],
                    "size": hk["size"], "color": hk["color"], "y": hk["y_percent"]}
        (out_dir / f"{name}.ass").write_text(build_ass(words, p.settings["style"], hook), encoding="utf-8")
        (out_dir / f"{name}.srt").write_text(build_srt(words, p.settings["style"]), encoding="utf-8")
        p.set_short(s["index"], subtitles="done")
        p.set_step("subtitles", "running", f"Subtitles {n + 1}/{len(shorts)}", (n + 1) / len(shorts))
