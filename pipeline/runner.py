"""Runs the pipeline steps in order, skipping finished ones (this is what makes resume work)."""
import traceback

from . import analyze, render, subtitles, transcribe
from .media import Cancelled, probe
from .project import STEPS


def step_ingest(ctx):
    p = ctx.project
    path = p.source_path()
    if not path.is_file():
        raise RuntimeError(f"Source video not found: {path}")
    if not p.data["source"].get("duration"):
        p.data["source"].update(probe(path))
        p.save()
    p.set_step("ingest", "running", "Source ready", 1)


STEP_FUNCS = {
    "ingest": step_ingest,
    "transcribe": transcribe.run,
    "analyze": analyze.run,
    "cut": render.run_cut,
    "subtitles": subtitles.run,
    "render": render.run_render,
}


def run_pipeline(ctx):
    p = ctx.project
    current = None
    p.set_state("running")
    try:
        for step in STEPS:
            if p.step_status(step) == "done":
                continue
            ctx.check()
            current = step
            p.set_step(step, "running", "Starting...", 0)
            STEP_FUNCS[step](ctx)
            p.set_step(step, "done", progress=1)
        p.set_state("done")
    except Cancelled:
        if current:
            p.set_step(current, "pending", "Cancelled - resume to continue")
        p.set_state("cancelled")
    except Exception as e:  # keep the message visible in the UI and in a log
        if current:
            p.set_step(current, "error", str(e)[-600:])
        (p.dir / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        p.set_state("error")
