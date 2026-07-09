#!/usr/bin/env python3
"""Local-only audio/video transcription to WebVTT for Hermes /vtt."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Iterable


def ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    millis = int(round(seconds * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{millis:03}"


def write_vtt(segments: Iterable[dict], output: pathlib.Path) -> None:
    lines = ["WEBVTT", ""]
    for index, seg in enumerate(segments, 1):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(str(index))
        lines.append(f"{ts(float(seg['start']))} --> {ts(float(seg['end']))}")
        lines.append(text)
        lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")


def progress(event: str, **fields) -> None:
    payload = {"event": event, **fields}
    print("HERMES_PROGRESS " + json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def elapsed(args: argparse.Namespace) -> float:
    return time.time() - float(getattr(args, "started_at", time.time()))


def eta_from_percent(args: argparse.Namespace, percent: float) -> float | None:
    if percent <= 0:
        return None
    done = min(max(percent, 0.1), 99.9) / 100.0
    spent = elapsed(args)
    return max((spent / done) - spent, 0.0)


def probe_duration(path: pathlib.Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    cmd = [
        exe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return float(proc.stdout.strip())
    except Exception:
        return None


def rough_eta_seconds(args: argparse.Namespace, duration: float | None) -> float | None:
    if not duration or duration <= 0:
        return None
    model_factor = {
        "tiny": 0.18,
        "base": 0.32,
        "small": 0.55,
        "medium": 0.9,
        "large": 1.25,
    }
    factor = 0.7
    model_name = str(args.model).lower()
    for prefix, value in model_factor.items():
        if model_name.startswith(prefix):
            factor = value
            break
    if args.backend == "whisper":
        factor *= 1.35
    elif args.backend == "whisper-cli":
        factor *= 1.5
    device = str(args.device).lower()
    if device == "cpu":
        factor *= 1.25
    elif device in {"cuda", "mps"}:
        factor *= 0.75
    return max(duration * factor, 15.0)


def transcribe_faster_whisper(args: argparse.Namespace) -> list[dict]:
    from faster_whisper import WhisperModel  # type: ignore

    progress(
        "backend",
        status="loading faster-whisper",
        backend="faster-whisper",
        model=args.model,
        environment=f"local STT (faster-whisper, device {args.device})",
        elapsed_sec=round(elapsed(args), 1),
    )
    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
    )
    progress(
        "backend",
        status="transcribing",
        backend="faster-whisper",
        model=args.model,
        environment=f"local STT (faster-whisper, device {args.device})",
        elapsed_sec=round(elapsed(args), 1),
        eta_sec=rough_eta_seconds(args, getattr(args, "duration_seconds", None)),
    )
    segments, _info = model.transcribe(
        str(args.input),
        language=args.language,
        task=args.task,
        vad_filter=args.vad,
        beam_size=args.beam_size,
    )
    duration = getattr(_info, "duration", None) or getattr(args, "duration_seconds", None)
    collected: list[dict] = []
    last_percent = -1.0
    last_sent = 0.0
    for seg in segments:
        item = {"start": seg.start, "end": seg.end, "text": seg.text}
        collected.append(item)
        if duration:
            percent = min(round((float(seg.end) / float(duration)) * 100, 1), 99.0)
            now = time.time()
            if percent >= last_percent + 10 or now - last_sent >= 30:
                progress(
                    "progress",
                    status="transcribing",
                    backend="faster-whisper",
                    model=args.model,
                    environment=f"local STT (faster-whisper, device {args.device})",
                    segment=len(collected),
                    percent=percent,
                    elapsed_sec=round(elapsed(args), 1),
                    eta_sec=round(eta_from_percent(args, percent) or 0, 1),
                )
                last_percent = percent
                last_sent = now
    return collected


def transcribe_openai_whisper(args: argparse.Namespace) -> list[dict]:
    import whisper  # type: ignore

    progress(
        "backend",
        status="loading openai-whisper",
        backend="whisper",
        model=args.model,
        environment="local STT (openai-whisper)",
        elapsed_sec=round(elapsed(args), 1),
    )
    model = whisper.load_model(args.model)
    progress(
        "backend",
        status="transcribing",
        backend="whisper",
        model=args.model,
        environment="local STT (openai-whisper)",
        elapsed_sec=round(elapsed(args), 1),
        eta_sec=rough_eta_seconds(args, getattr(args, "duration_seconds", None)),
    )
    result = model.transcribe(
        str(args.input),
        language=args.language,
        task=args.task,
        verbose=False,
    )
    return [
        {"start": seg["start"], "end": seg["end"], "text": seg["text"]}
        for seg in result.get("segments", [])
    ]


def transcribe_whisper_cli(args: argparse.Namespace, output: pathlib.Path) -> bool:
    exe = shutil.which("whisper")
    if not exe:
        return False
    progress(
        "backend",
        status="running whisper CLI",
        backend="whisper-cli",
        model=args.model,
        environment="local STT (whisper CLI)",
        elapsed_sec=round(elapsed(args), 1),
        eta_sec=rough_eta_seconds(args, getattr(args, "duration_seconds", None)),
    )
    with tempfile.TemporaryDirectory(prefix="hermes-vtt-") as tmp:
        cmd = [
            exe,
            str(args.input),
            "--model",
            args.model,
            "--task",
            args.task,
            "--output_format",
            "vtt",
            "--output_dir",
            tmp,
        ]
        if args.language:
            cmd.extend(["--language", args.language])
        subprocess.run(cmd, check=True)
        candidates = sorted(pathlib.Path(tmp).glob("*.vtt"))
        if not candidates:
            raise RuntimeError("whisper CLI completed but did not create a VTT file")
        shutil.copy2(candidates[0], output)
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local STT to WebVTT.")
    parser.add_argument("input", type=pathlib.Path, help="Audio/video file path")
    parser.add_argument("-o", "--output", type=pathlib.Path, help="Output .vtt path")
    parser.add_argument("--model", default="small", help="Local Whisper model name/path")
    parser.add_argument("--language", default=None, help="Language code, or auto-detect")
    parser.add_argument("--task", choices=("transcribe", "translate"), default="transcribe")
    parser.add_argument("--backend", choices=("auto", "faster-whisper", "whisper", "whisper-cli"), default="auto")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--vad", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.started_at = time.time()
    args.input = args.input.expanduser().resolve()
    if not args.input.exists():
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 2
    output = args.output.expanduser().resolve() if args.output else args.input.with_suffix(".vtt")
    output.parent.mkdir(parents=True, exist_ok=True)
    args.duration_seconds = probe_duration(args.input)
    eta = rough_eta_seconds(args, args.duration_seconds)

    progress(
        "start",
        task="vtt",
        model=args.model,
        environment=f"local STT ({args.backend}, device {args.device})",
        backend=args.backend,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language or "auto",
        input=str(args.input),
        output=str(output),
        duration_sec=round(args.duration_seconds, 1) if args.duration_seconds else None,
        eta_seconds=round(eta, 1) if eta else None,
    )

    errors: list[str] = []
    if args.backend in ("auto", "faster-whisper"):
        try:
            write_vtt(transcribe_faster_whisper(args), output)
            progress(
                "complete",
                task="vtt",
                model=args.model,
                environment=f"local STT (faster-whisper, device {args.device})",
                output=str(output),
                elapsed_sec=round(elapsed(args), 1),
            )
            print(str(output))
            return 0
        except Exception as exc:
            errors.append(f"faster-whisper: {exc}")
            progress(
                "backend",
                status="faster-whisper failed; trying next backend",
                backend="faster-whisper",
                model=args.model,
                environment=f"local STT (faster-whisper, device {args.device})",
                elapsed_sec=round(elapsed(args), 1),
            )
            if args.backend == "faster-whisper":
                break_backend = True
            else:
                break_backend = False
            if break_backend:
                print("\n".join(errors), file=sys.stderr)
                return 1

    if args.backend in ("auto", "whisper"):
        try:
            write_vtt(transcribe_openai_whisper(args), output)
            progress(
                "complete",
                task="vtt",
                model=args.model,
                environment="local STT (openai-whisper)",
                output=str(output),
                elapsed_sec=round(elapsed(args), 1),
            )
            print(str(output))
            return 0
        except Exception as exc:
            errors.append(f"whisper python: {exc}")
            progress(
                "backend",
                status="openai-whisper failed; trying next backend",
                backend="whisper",
                model=args.model,
                environment="local STT (openai-whisper)",
                elapsed_sec=round(elapsed(args), 1),
            )
            if args.backend == "whisper":
                print("\n".join(errors), file=sys.stderr)
                return 1

    if args.backend in ("auto", "whisper-cli"):
        try:
            if transcribe_whisper_cli(args, output):
                progress(
                    "complete",
                    task="vtt",
                    model=args.model,
                    environment="local STT (whisper CLI)",
                    output=str(output),
                    elapsed_sec=round(elapsed(args), 1),
                )
                print(str(output))
                return 0
            errors.append("whisper CLI: executable not found")
        except Exception as exc:
            errors.append(f"whisper CLI: {exc}")

    progress(
        "error",
        task="vtt",
        model=args.model,
        environment=f"local STT ({args.backend}, device {args.device})",
        message="No local STT backend succeeded.",
        elapsed_sec=round(elapsed(args), 1),
    )
    print("No local STT backend succeeded.", file=sys.stderr)
    print("Install one of: faster-whisper, openai-whisper, or the whisper CLI.", file=sys.stderr)
    print("\n".join(errors), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
