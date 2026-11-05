"""Step 3: Gemini picks the most viral segments; results are validated and snapped to word boundaries."""
import bisect
import json
import time

from .transcribe import load as load_transcript

MAX_CHARS = 350_000  # transcript chars per Gemini call; longer videos are split into windows

# ---------------------------------------------------------------------------
# HARDCODED PROMPT - edit here to change what "viral" means.
# Tokens replaced at runtime: <<COUNT>> <<MIN>> <<MAX>> <<TRANSCRIPT>>
# ---------------------------------------------------------------------------
VIRAL_PROMPT = """You are an expert short-form video editor who turns long Minecraft videos (survival, SMP/multiplayer, challenges, hardcore, speedruns, builds, redstone, PvP) into viral YouTube Shorts.

Below is a timestamped transcript of one video. Each line is "[start-end] text" with times in seconds.
Select the <<COUNT>> best moments to turn into standalone Shorts.

What makes a Minecraft moment viral (prefer these):
- A strong hook in the first 3 seconds: a bold claim, a question, shouting, a surprise, or an immediate problem.
- Clutch saves, near-death escapes, last-second wins, impossible-looking luck.
- Funny fails, deaths, trolling, pranks, betrayals, and chaotic banter between players.
- Big payoffs: rare finds, boss fights, raids, huge builds or redstone contraptions being revealed, successful gambles.
- Strong emotional reactions: screaming, laughing, disbelief, rage, celebration.
- Quotable one-liners and punchlines. A clear mini-story: setup, tension, payoff.
- Cliffhangers or "wait for it" moments that make people watch to the end.

Avoid:
- Silence, menus, inventory management, long travelling, idle mining or farming without commentary.
- Moments that need outside context to understand, and rambling explanations without action.
- Starting or ending mid-sentence. Start at the beginning of a sentence and end after the payoff finishes.

Rules:
- Every Short must be between <<MIN>> and <<MAX>> seconds long (end minus start). Never exceed <<MAX>> seconds.
- Shorts must not overlap each other.
- Use the exact second values from the transcript timestamps for start and end.
- Give each Short a catchy title (max 8 words) and a viral score from 1 to 100.
- "hook": 3 to 6 words of bold on-screen text shown in the first 2 seconds that creates curiosity or states the payoff (e.g. "HE SURVIVED THIS?!"). No hashtags, no emojis.
- "peaks": up to 3 timestamps in seconds (inside start and end, at least 3 seconds apart, not in the first 2 seconds) of the funniest or most intense moments, where a quick zoom would land.
- "description": one or two sentences for the YouTube description. "hashtags": 5 to 8 relevant hashtags starting with # (always include #minecraft and #shorts).

Return ONLY a JSON array, no prose, in this format:
[{"start": 123.4, "end": 171.0, "title": "...", "score": 92, "reason": "one sentence why it works", "hook": "...", "peaks": [140.2, 155.8], "description": "...", "hashtags": ["#minecraft", "#shorts"]}]

TRANSCRIPT:
<<TRANSCRIPT>>
"""


def list_models(api_key):
    from google import genai
    client = genai.Client(api_key=api_key)
    names = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if "generateContent" in actions and "gemini" in m.name:
            names.append(m.name.replace("models/", ""))
    return sorted(set(names), reverse=True)


def _windows(lines):
    chunks, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) > MAX_CHARS and cur:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append(cur)
    return chunks


RETRY_WAITS = [5, 15, 30, 60, 90]  # seconds to wait between tries while Google is overloaded
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
TEMPORARY = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL", "502", "504", "DEADLINE",
             "overloaded", "timed out", "Connection")


class _Overloaded(Exception):
    pass


def _temporary(e):
    """Worth retrying: overload, rate limit, network trouble, or a malformed JSON reply."""
    return isinstance(e, ValueError) or any(k in str(e) for k in TEMPORARY)


def _wait(ctx, seconds, msg):
    for left in range(int(seconds), 0, -1):
        ctx.check()
        if left % 5 == 0 or left == int(seconds):
            ctx.project.set_step("analyze", "running", f"{msg} - retrying in {left}s")
        time.sleep(1)


def _call(ctx, client, types, prompt, model, raw_path, waits):
    last = None
    for i in range(len(waits) + 1):
        ctx.check()
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.3))
            raw_path.write_text(resp.text or "", encoding="utf-8")
            data = json.loads(resp.text)
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [])
            return data
        except Exception as e:
            last = e
            if not _temporary(e):  # e.g. invalid key or unknown model: waiting will not help
                raise RuntimeError(f"Gemini error with {model}: {str(e)[:300]}")
            if i < len(waits):
                _wait(ctx, waits[i], f"Gemini busy ({model})")
    raise _Overloaded(str(last))


def _ask(ctx, prompt, model, raw_path):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=ctx.api_key)
    try:
        return _call(ctx, client, types, prompt, model, raw_path, RETRY_WAITS)
    except _Overloaded as e:
        last = e
    try:
        available = set(list_models(ctx.api_key))
    except Exception:
        available = set()
    for alt in FALLBACK_MODELS:  # the chosen model stays overloaded: try another one
        if alt == model or (available and alt not in available):
            continue
        ctx.project.set_step("analyze", "running", f"{model} is overloaded - trying {alt}")
        try:
            return _call(ctx, client, types, prompt, alt, raw_path, RETRY_WAITS[:2])
        except _Overloaded as e:
            last = e
    raise RuntimeError("Gemini is overloaded right now (503). Wait a few minutes and press Resume - finished steps "
                       "are kept. Last error: " + str(last)[:200])


def _nearest(values, target, tol):
    i = bisect.bisect_left(values, target)
    best = min((values[j] for j in (i - 1, i) if 0 <= j < len(values)),
               key=lambda v: abs(v - target), default=None)
    return best if best is not None and abs(best - target) <= tol else target


def snap_and_select(cands, words, count, min_len, max_len, total):
    starts = sorted(w["start"] for w in words)
    ends = sorted(w["end"] for w in words)
    limit = max_len - 0.5  # room for padding
    ok = []
    for c in cands:
        try:
            s, e = float(c["start"]), float(c["end"])
            score = float(c.get("score", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        if starts:
            s = _nearest(starts, s, 4.0)
            e = _nearest(ends, e, 4.0)
            if e - s > limit:  # trim to last word end that fits
                i = bisect.bisect_right(ends, s + limit) - 1
                e = ends[i] if i >= 0 and ends[i] > s else s + limit
            if e - s < min_len:  # extend to the first word end that reaches min length
                i = bisect.bisect_left(ends, s + min_len)
                if i < len(ends) and ends[i] - s <= limit:
                    e = ends[i]
        s, e = max(0.0, s - 0.15), min(total, e + 0.35)
        if e - s < min_len - 0.5 or e - s > max_len:
            continue
        peaks = []
        for x in (c.get("peaks") or []) if isinstance(c.get("peaks"), list) else []:
            try:
                if s + 1.5 <= float(x) <= e - 1.5:
                    peaks.append(round(float(x), 2))
            except (TypeError, ValueError):
                pass
        tags = [str(t).strip() for t in (c.get("hashtags") or []) if str(t).strip()] if isinstance(c.get("hashtags"), list) else []
        ok.append({"start": round(s, 2), "end": round(e, 2), "title": str(c.get("title", "Untitled"))[:80],
                   "score": score, "reason": str(c.get("reason", ""))[:300], "hook": str(c.get("hook", ""))[:60],
                   "peaks": peaks[:3], "description": str(c.get("description", ""))[:500], "hashtags": tags[:10]})
    ok.sort(key=lambda x: -x["score"])
    chosen = []
    for c in ok:  # greedy: best score first, skip overlaps
        if all(min(c["end"], o["end"]) - max(c["start"], o["start"]) <= 0 for o in chosen):
            chosen.append(c)
        if len(chosen) == count:
            break
    return chosen


def run(ctx):
    p = ctx.project
    st = p.settings
    if not ctx.api_key:
        raise RuntimeError("Gemini API key missing - enter it in the settings and press Start/Resume")
    if not st["gemini_model"]:
        raise RuntimeError("No Gemini model selected - load the model list and pick one")

    tr = load_transcript(p)
    out_dir = p.step_dir("analyze")
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in tr["segments"]]
    if not lines:
        raise RuntimeError("Transcript is empty - nothing to analyze")

    chunks = _windows(lines)
    want = min(40, max(st["count"] * 2, st["count"] + 3))  # over-ask, then filter
    per = -(-want // len(chunks))
    cands = []
    for i, chunk in enumerate(chunks):
        p.set_step("analyze", "running", f"Asking Gemini ({i + 1}/{len(chunks)})", i / len(chunks))
        prompt = (VIRAL_PROMPT.replace("<<COUNT>>", str(per)).replace("<<MIN>>", str(st["min_len"]))
                  .replace("<<MAX>>", str(st["max_len"])).replace("<<TRANSCRIPT>>", "\n".join(chunk)))
        cands += _ask(ctx, prompt, st["gemini_model"], out_dir / f"gemini_raw_{i + 1}.json")

    words = [w for s in tr["segments"] for w in s["words"]]
    chosen = snap_and_select(cands, words, st["count"], st["min_len"], st["max_len"], tr["duration"])
    if not chosen:
        raise RuntimeError("Gemini returned no usable segments (check length limits or try again)")

    shorts = [{"index": i + 1, **c, "cut": "pending", "subtitles": "pending", "render": "pending"}
              for i, c in enumerate(chosen)]
    (out_dir / "segments.json").write_text(
        json.dumps({"model": st["gemini_model"], "candidates": len(cands), "shorts": shorts}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    with p.lock:
        p.data["shorts"] = shorts
        p.save()
    msg = f"Selected {len(shorts)} of {st['count']} requested"
    if len(shorts) < st["count"]:
        msg += " (not enough non-overlapping segments in the length range)"
    p.set_step("analyze", "running", msg, 1)
