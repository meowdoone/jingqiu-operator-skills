#!/usr/bin/env python3
"""Local pixel-event inspection and dependency-aware repair planning. No model calls."""
import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess


def positive(value, name, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f"{name}: expected {'nonnegative' if zero else 'positive'} finite number")
    return value


def parse_metadata(text):
    """FFmpeg metadata is observation, not semantic scene or defect classification."""
    frames, last_time, cuts, freezes, active = 0, None, [], [], None
    for line in text.splitlines():
        match = re.match(r"frame:\s*(\d+).*pts_time:([\d.eE+-]+)", line)
        if match:
            frames += 1
            last_time = float(match[2])
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "lavfi.scd.time":
            cuts.append(float(value))
        elif key == "lavfi.freezedetect.freeze_start":
            active = {"start": float(value), "end": None}
            freezes.append(active)
        elif key == "lavfi.freezedetect.freeze_end" and active is not None:
            active["end"] = float(value)
            active = None
    return {"decoded_frames": frames, "last_frame_time": last_time,
            "cut_candidates": cuts, "low_change_intervals": freezes}


def inspect_video(filename, seconds=60, scene_threshold=10, freeze_seconds=1, freeze_noise=0.001):
    source = Path(filename).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        raise ValueError("Use one local video file, not a URL, playlist or directory")
    for value, name in [(seconds, "seconds"), (scene_threshold, "scene_threshold"), (freeze_seconds, "freeze_seconds"), (freeze_noise, "freeze_noise")]:
        positive(value, name)
    if seconds > 600 or scene_threshold > 100 or freeze_noise > 1:
        raise ValueError("Maximum scan 600 seconds; scene threshold <=100; freeze noise <=1")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("FFmpeg is required for inspect; plan needs only Python")
    filters = (f"setpts=PTS-STARTPTS,trim=duration={seconds},scale=w='min(640,iw)':h=-2,"
               f"scdet=threshold={scene_threshold},freezedetect=n={freeze_noise}:d={freeze_seconds},"
               "metadata=mode=print:file=-")
    args = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror",
            "-protocol_whitelist", "file,pipe", "-i", str(source), "-t", str(seconds),
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", filters, "-f", "null", "-"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=180, check=True)
    observation = parse_metadata(result.stdout)
    if not observation["decoded_frames"]:
        raise ValueError("No video frames observed; inspect filter/decoder output")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    version = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    return {"mode": "pixel_event_inspection", "source_name": source.name, "source_sha256": digest.hexdigest(),
            "engine": version, "parameters": {"scan_limit_seconds": seconds, "scene_threshold": scene_threshold,
            "freeze_seconds": freeze_seconds, "freeze_noise": freeze_noise, "analysis_max_width": 640},
            **observation, "source_modified": False,
            "limits": "First video stream only; relative timestamps. No audio/identity/product/quality score. Cut and low-change candidates require review. Null interval end means not observed before scan end; scan limit is not source duration."}


def repair_plan(data):
    """Propagate stale references through a DAG, then apply a total retry-budget gate."""
    shots = data.get("shots")
    changed = data.get("changed")
    budget = positive(data.get("budget_remaining"), "budget_remaining", zero=True)
    if not isinstance(shots, list) or not shots or not isinstance(changed, list):
        raise ValueError("shots must be nonempty; changed must be a list")
    by_id = {}
    for shot in shots:
        sid = shot.get("id")
        if not isinstance(sid, str) or not sid.strip() or sid in by_id:
            raise ValueError("Shot IDs must be nonempty unique strings")
        deps = shot.get("depends_on")
        if not isinstance(deps, list) or any(not isinstance(x, str) for x in deps) or len(set(deps)) != len(deps):
            raise ValueError("depends_on must contain unique shot IDs")
        positive(shot.get("cost_per_attempt"), "cost_per_attempt", zero=True)
        attempts = shot.get("attempts_remaining")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ValueError("attempts_remaining must be a nonnegative integer")
        by_id[sid] = shot
    if any(not isinstance(x, str) or x not in by_id for x in changed) or len(set(changed)) != len(changed):
        raise ValueError("changed must contain unique known shot IDs")
    for sid, shot in by_id.items():
        if any(dep not in by_id or dep == sid for dep in shot["depends_on"]):
            raise ValueError("Unknown or self-referencing dependency")
    order, remaining = [], set(by_id)
    while remaining:
        ready = [sid for sid in by_id if sid in remaining and all(dep in order for dep in by_id[sid]["depends_on"])]
        if not ready:
            raise ValueError("Dependency cycle")
        order.extend(ready)
        remaining.difference_update(ready)
    stale = set(changed)
    for sid in order:
        if any(dep in stale for dep in by_id[sid]["depends_on"]):
            stale.add(sid)
    repair = [sid for sid in order if sid in stale]
    cost = sum((Decimal(str(by_id[sid]["cost_per_attempt"])) for sid in repair), Decimal(0))
    exhausted = [sid for sid in repair if by_id[sid]["attempts_remaining"] == 0]
    status = "NO_CHANGE" if not repair else "NEEDS_PRODUCTION_DECISION" if exhausted or cost > Decimal(str(budget)) else "READY_FOR_REVIEW"
    return {"mode": "repair_plan", "status": status, "changed": changed, "recheck_or_rebuild": repair,
            "unaffected_by_declared_changes": [sid for sid in order if sid not in stale], "retry_exhausted": exhausted,
            "one_attempt_estimate": float(cost), "budget_remaining": budget, "executes_generation": False,
            "limits": "Declared render-reference edges only, not edit order. Descendants need rechecking, not unconditional rerendering. Costs share one user-defined unit; no generation or approval implied."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    inspect = modes.add_parser("inspect")
    inspect.add_argument("file")
    inspect.add_argument("--seconds", type=float, default=60)
    inspect.add_argument("--scene-threshold", type=float, default=10)
    inspect.add_argument("--freeze-seconds", type=float, default=1)
    inspect.add_argument("--freeze-noise", type=float, default=0.001)
    plan = modes.add_parser("plan")
    plan.add_argument("file")
    args = parser.parse_args()
    try:
        if args.mode == "inspect":
            result = inspect_video(args.file, args.seconds, args.scene_threshold, args.freeze_seconds, args.freeze_noise)
        else:
            result = repair_plan(json.loads(Path(args.file).read_text()))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, OSError, subprocess.SubprocessError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Cannot complete {args.mode}: {exc}\n")


if __name__ == "__main__":
    main()
