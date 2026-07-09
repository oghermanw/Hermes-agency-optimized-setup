from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import signal
import shlex
import subprocess
import sys
import threading
import time
import uuid


HERMES_HOME = pathlib.Path(os.environ.get("HERMES_HOME", pathlib.Path.home() / ".hermes")).expanduser()
BIN_DIR = HERMES_HOME / "bin"
CACHE_DIR = HERMES_HOME / "cache" / "hermes-agency-workers"
LOG_DIR = HERMES_HOME / "logs" / "hermes-agency-workers"
JOBS_DIR = CACHE_DIR / "jobs"
QUEUE_STATE_PATH = CACHE_DIR / "queue-state.json"

_PROGRESS_PREFIX = "HERMES_PROGRESS "
_HEAVY_JOB_NAMES = {"ebook", "vtt"}
_UNFINISHED_STATUSES = {"queued", "running", "cancel_requested"}
_QUEUE_LOCK = threading.RLock()
_QUEUE_WORKER: threading.Thread | None = None
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|auth[_-]?token|access[_-]?token|authorization|bot[_-]?token)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
]


def _split(raw_args: str) -> list[str]:
    try:
        return shlex.split(raw_args or "")
    except ValueError as exc:
        raise RuntimeError(f"Could not parse arguments: {exc}") from exc


def _trim_output(name: str, text: str, limit: int = 3600) -> str:
    text = _sanitize_text(text)
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "Command returned no output."
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"{name}-{stamp}.log"
    log_path.write_text(text, encoding="utf-8")
    os.chmod(log_path, 0o600)
    return text[:limit] + f"\n\n[Output truncated. Full log: {log_path}]"


def _sanitize_text(text: str) -> str:
    text = text or ""
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text)
    except Exception:
        pass
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}=***", text)
    return text


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        upper = key.upper()
        if (
            upper.endswith("_API_KEY")
            or upper.endswith("_TOKEN")
            or upper in {"OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "AUTHORIZATION"}
        ):
            env.pop(key, None)
    env.setdefault("HERMES_HOME", str(HERMES_HOME))
    return env


def _safe_name(path_or_text: str, limit: int = 80) -> str:
    value = (path_or_text or "").strip()
    if not value:
        return ""
    try:
        path = pathlib.Path(value).expanduser()
        value = path.name or str(path)
    except Exception:
        pass
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def _fmt_seconds(seconds: object) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "estimating"
    if total <= 0:
        return "estimating"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _parse_option(args: list[str], names: set[str], default: str | None = None) -> str | None:
    for index, arg in enumerate(args):
        if arg in names and index + 1 < len(args):
            return args[index + 1]
        for name in names:
            prefix = f"{name}="
            if arg.startswith(prefix):
                return arg[len(prefix):]
    return default


def _non_option_args(args: list[str]) -> list[str]:
    result: list[str] = []
    skip_next = False
    options_with_values = {"--format", "--style", "--model", "--language", "--backend", "--device", "--compute-type", "--beam-size"}
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in options_with_values:
            skip_next = True
            continue
        if any(arg.startswith(f"{opt}=") for opt in options_with_values):
            continue
        if arg.startswith("-"):
            continue
        result.append(arg)
    return result


def _looks_like_output_path(value: str) -> bool:
    suffix = pathlib.Path(value).suffix.lower()
    return suffix in {".epub", ".mobi", ".azw3", ".md", ".txt"} or "/" in value or value.startswith("~")


def _job_id(name: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{name}-{stamp}-{uuid.uuid4().hex[:6]}"


def _job_path(job_id: str) -> pathlib.Path:
    return JOBS_DIR / f"{job_id}.json"


def _write_job(job: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = _job_path(job["job_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read_job(job_id: str) -> dict | None:
    path = _job_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_all_jobs() -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    jobs: list[dict] = []
    for path in JOBS_DIR.glob("*.json"):
        job = _read_job(path.stem)
        if job:
            jobs.append(job)
    return jobs


def _job_sort_key(job: dict) -> tuple[float, str]:
    value = job.get("queue_index") or job.get("created_at") or ""
    if isinstance(value, (int, float)):
        return (float(value), str(job.get("job_id", "")))
    try:
        parsed = _dt.datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        parsed = 0.0
    return (parsed, str(job.get("job_id", "")))


def _read_queue_state() -> dict:
    if not QUEUE_STATE_PATH.exists():
        return {"paused": False}
    try:
        state = json.loads(QUEUE_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {"paused": False}
    except Exception:
        return {"paused": False}


def _write_queue_state(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(QUEUE_STATE_PATH)


def _queue_paused() -> bool:
    return bool(_read_queue_state().get("paused"))


def _set_queue_paused(paused: bool) -> None:
    state = _read_queue_state()
    state["paused"] = bool(paused)
    state["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    _write_queue_state(state)


def _target_key(target: dict | None) -> str:
    target = target or {}
    return "|".join(
        [
            str(target.get("platform") or ""),
            str(target.get("chat_id") or ""),
            str(target.get("thread_id") or ""),
        ]
    )


def _same_target(job: dict, target_key: str) -> bool:
    return str(job.get("target_key") or _target_key(job.get("target"))) == target_key


def _heavy_jobs(statuses: set[str] | None = None, target_key: str | None = None) -> list[dict]:
    jobs = [job for job in _read_all_jobs() if job.get("queue") == "local-heavy"]
    if statuses is not None:
        jobs = [job for job in jobs if job.get("status") in statuses]
    if target_key is not None:
        jobs = [job for job in jobs if _same_target(job, target_key)]
    return sorted(jobs, key=_job_sort_key)


def _active_batch_id_for_target(target_key: str) -> str | None:
    for job in _heavy_jobs(_UNFINISHED_STATUSES, target_key):
        batch_id = job.get("batch_id")
        if batch_id:
            return str(batch_id)
    return None


def _new_batch_id() -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"batch-{stamp}-{uuid.uuid4().hex[:6]}"


def _queue_position(job_id: str, target_key: str | None = None) -> int | None:
    queued = _heavy_jobs({"queued"}, target_key)
    for index, job in enumerate(queued, 1):
        if job.get("job_id") == job_id:
            return index
    return None


def _running_heavy_job() -> dict | None:
    running = _heavy_jobs({"running", "cancel_requested"})
    return running[0] if running else None


def _pid_alive(pid: object) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _session_target() -> dict[str, str]:
    try:
        from gateway.session_context import get_session_env

        return {
            "platform": get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower(),
            "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
            "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", "").strip(),
            "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
        }
    except Exception:
        return {"platform": "", "chat_id": "", "thread_id": "", "message_id": ""}


def _deliver_update(job: dict, message: str) -> bool:
    target = job.get("target") or {}
    platform_name = (target.get("platform") or "").strip().lower()
    chat_id = (target.get("chat_id") or "").strip()
    if not platform_name or not chat_id:
        return False

    message = _sanitize_text(message)
    try:
        from gateway.config import Platform
        from gateway.delivery import DeliveryTarget
        from gateway.run import _gateway_runner_ref
        from model_tools import _run_async

        runner = _gateway_runner_ref()
        if runner is None:
            return False
        router = runner.delivery_router
        router.adapters = runner.adapters

        metadata = {
            "job_id": job.get("job_id"),
            "job_name": f"/{job.get('name', 'agency-job')}",
        }
        if platform_name == "telegram" and target.get("message_id"):
            metadata["telegram_reply_to_message_id"] = str(target["message_id"])

        delivery_target = DeliveryTarget(
            platform=Platform(platform_name),
            chat_id=chat_id,
            thread_id=target.get("thread_id") or None,
            is_explicit=True,
        )
        result = _run_async(
            router.deliver(
                message,
                [delivery_target],
                job_id=job.get("job_id"),
                job_name=f"/{job.get('name', 'agency-job')}",
                metadata=metadata,
            )
        )
        return bool(result)
    except Exception as exc:
        job.setdefault("delivery_errors", []).append(_sanitize_text(str(exc))[:300])
        return False


def _format_start(job: dict) -> str:
    meta = job.get("meta") or {}
    lines = [
        f"/{job['name']} started",
        f"Job: {job['job_id']}",
        f"Model: {meta.get('model', 'local')}",
        f"Environment: {meta.get('environment', 'local')}",
    ]
    if meta.get("input"):
        lines.append(f"Input: {_safe_name(str(meta['input']))}")
    if meta.get("target_language"):
        lines.append(f"Target: {meta['target_language']}")
    if meta.get("output_format"):
        lines.append(f"Output format: {meta['output_format']}")
    if meta.get("style"):
        lines.append(f"Style: {meta['style']}")
    if meta.get("eta_seconds"):
        lines.append(f"ETA: about {_fmt_seconds(meta['eta_seconds'])}")
    else:
        lines.append("ETA: estimating after first progress signal")
    return "\n".join(lines)


def _format_progress(job: dict, payload: dict) -> str | None:
    event = payload.get("event")
    name = job.get("name", "job")
    meta = job.get("meta") or {}
    if event == "start":
        job["meta"].update({k: v for k, v in payload.items() if k != "event" and v not in (None, "")})
        return _format_start(job)

    if event in {"backend", "progress"}:
        status = payload.get("status") or payload.get("backend") or "running"
        percent = payload.get("percent")
        chunk = payload.get("chunk")
        total = payload.get("total")
        elapsed = _fmt_seconds(payload.get("elapsed_sec"))
        eta = _fmt_seconds(payload.get("eta_sec"))
        parts = [f"/{name} progress: {status}"]
        if chunk and total:
            parts.append(f"chunk {chunk}/{total}")
        if percent is not None:
            parts.append(f"{percent}%")
        lines = [
            " | ".join(parts),
            f"Elapsed: {elapsed} | ETA: {eta}",
            f"Model: {payload.get('model') or meta.get('model', 'local')}",
            f"Environment: {payload.get('environment') or meta.get('environment', 'local')}",
        ]
        return "\n".join(lines)

    if event == "complete":
        output = payload.get("output") or job.get("output")
        lines = [
            f"/{name} complete",
            f"Job: {job['job_id']}",
            f"Elapsed: {_fmt_seconds(payload.get('elapsed_sec') or job.get('elapsed_sec'))}",
        ]
        if output:
            lines.append(f"Output: {output}")
        return "\n".join(lines)

    if event == "error":
        detail = payload.get("message") or payload.get("error") or "unknown error"
        return f"/{name} error\nJob: {job['job_id']}\n{_sanitize_text(str(detail))}"

    return None


def _should_send_progress(job: dict, payload: dict) -> bool:
    event = payload.get("event")
    if event in {"start", "complete", "error", "backend"}:
        return True
    if event != "progress":
        return False

    now = time.time()
    last_at = float(job.get("last_progress_sent_at") or 0)
    last_percent = float(job.get("last_progress_percent") or -1)
    try:
        percent = float(payload.get("percent"))
    except (TypeError, ValueError):
        percent = last_percent
    chunk = payload.get("chunk")
    total = payload.get("total")
    if chunk is not None and total is not None and (chunk == 1 or chunk == total):
        return True
    if percent >= last_percent + 10:
        return True
    return now - last_at >= 30


def _note_progress_sent(job: dict, payload: dict) -> None:
    job["last_progress_sent_at"] = time.time()
    try:
        job["last_progress_percent"] = float(payload.get("percent"))
    except (TypeError, ValueError):
        pass


def _append_log(log_path: pathlib.Path, line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(_sanitize_text(line))
        if not line.endswith("\n"):
            fh.write("\n")


def _format_queued(job: dict, position: int | None) -> str:
    meta = job.get("meta") or {}
    running = _running_heavy_job()
    lines = [
        f"/{job['name']} queued",
        f"Job: {job['job_id']}",
        f"Queue: local-heavy",
    ]
    if position is not None:
        lines.append(f"Position: {position}")
    if running:
        lines.append(f"Running now: /{running.get('name')} {running.get('job_id')}")
    else:
        lines.append("Running now: will start next")
    lines.extend(
        [
            f"Model: {meta.get('model', 'local')}",
            f"Environment: {meta.get('environment', 'local')}",
        ]
    )
    if meta.get("input"):
        lines.append(f"Input: {_safe_name(str(meta['input']))}")
    if meta.get("output_format"):
        lines.append(f"Output format: {meta['output_format']}")
    if meta.get("style"):
        lines.append(f"Style: {meta['style']}")
    if _queue_paused():
        lines.append("Queue is paused; this will wait until /agency_resume.")
    return "\n".join(lines)


def _next_queued_heavy_job() -> dict | None:
    queued = _heavy_jobs({"queued"})
    return queued[0] if queued else None


def _ensure_queue_worker() -> None:
    global _QUEUE_WORKER
    with _QUEUE_LOCK:
        if _QUEUE_WORKER is not None and _QUEUE_WORKER.is_alive():
            return
        _QUEUE_WORKER = threading.Thread(target=_queue_worker_loop, daemon=True)
        _QUEUE_WORKER.start()


def _queue_worker_loop() -> None:
    idle_ticks = 0
    while True:
        if _queue_paused():
            time.sleep(2)
            idle_ticks += 1
            if idle_ticks > 1800:
                return
            continue

        running = _running_heavy_job()
        if running and _pid_alive(running.get("pid")):
            time.sleep(2)
            idle_ticks = 0
            continue
        if running and not _pid_alive(running.get("pid")):
            if running.get("status") == "cancel_requested":
                running["status"] = "cancelled"
                running["error"] = "Cancelled; process is no longer alive."
            else:
                running["status"] = "failed"
                running["error"] = "Queue recovered a stale running job whose process is no longer alive."
            running["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            running["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            _write_job(running)
            _maybe_report_batch_complete(running)

        job = _next_queued_heavy_job()
        if not job:
            return
        idle_ticks = 0
        argv = job.get("argv")
        if not isinstance(argv, list) or not argv:
            job["status"] = "failed"
            job["error"] = "Queued job is missing its command argv."
            job["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            _write_job(job)
            _maybe_report_batch_complete(job)
            continue
        _run_background_job(job, [str(part) for part in argv])
        _maybe_report_batch_complete(job)


def _batch_jobs(batch_id: str) -> list[dict]:
    return sorted(
        [job for job in _read_all_jobs() if job.get("batch_id") == batch_id],
        key=_job_sort_key,
    )


def _format_batch_summary(jobs: list[dict]) -> str:
    completed = sum(1 for job in jobs if job.get("status") == "complete")
    failed = sum(1 for job in jobs if job.get("status") == "failed")
    cancelled = sum(1 for job in jobs if job.get("status") == "cancelled")
    lines = [
        "Local queue complete",
        f"Total: {len(jobs)} | Complete: {completed} | Failed: {failed} | Cancelled: {cancelled}",
    ]
    for index, job in enumerate(jobs, 1):
        status = job.get("status", "?")
        label = f"{index}. /{job.get('name', '?')} {status}"
        output = job.get("output")
        error = job.get("error")
        if output:
            label += f" -> {output}"
        elif error and status == "failed":
            label += f" -> {_trim_output('queue-summary', str(error), limit=160)}"
        else:
            meta = job.get("meta") or {}
            if meta.get("input"):
                label += f" -> {_safe_name(str(meta['input']))}"
        lines.append(label)
    return "\n".join(lines)


def _maybe_report_batch_complete(job: dict) -> None:
    batch_id = job.get("batch_id")
    if not batch_id:
        return
    jobs = _batch_jobs(str(batch_id))
    if not jobs:
        return
    if any(item.get("status") in _UNFINISHED_STATUSES for item in jobs):
        return
    if all(item.get("batch_reported_at") for item in jobs):
        return

    report_job = dict(job)
    report_job["name"] = "agency-queue"
    _deliver_update(report_job, _format_batch_summary(jobs))

    reported_at = _dt.datetime.now().isoformat(timespec="seconds")
    for item in jobs:
        item["batch_reported_at"] = reported_at
        _write_job(item)


def _start_background_job(name: str, argv: list[str], meta: dict, timeout: int) -> str:
    job = {
        "job_id": _job_id(name),
        "name": name,
        "status": "queued",
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "target": _session_target(),
        "argv": argv,
        "argv_display": [pathlib.Path(argv[0]).name if argv else "", *argv[1:]],
        "meta": meta,
        "timeout": timeout,
    }
    if name in _HEAVY_JOB_NAMES:
        job["queue"] = "local-heavy"
        job["queue_index"] = time.time()
        job["target_key"] = _target_key(job["target"])
        job["batch_id"] = _active_batch_id_for_target(job["target_key"]) or _new_batch_id()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    job["log_path"] = str(LOG_DIR / f"{job['job_id']}.log")
    _write_job(job)

    if name in _HEAVY_JOB_NAMES:
        position = _queue_position(job["job_id"], job["target_key"])
        _ensure_queue_worker()
        return _format_queued(job, position)

    worker = threading.Thread(target=_run_background_job, args=(job, argv), daemon=True)
    worker.start()

    return (
        f"/{name} job started\n"
        f"Job: {job['job_id']}\n"
        f"Model: {meta.get('model', 'local')}\n"
        f"Environment: {meta.get('environment', 'local')}\n"
        "I will post progress here and then the final output."
    )


def _run_background_job(job: dict, argv: list[str]) -> None:
    started = time.time()
    log_path = pathlib.Path(job["log_path"])
    job["status"] = "running"
    job["started_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    job["updated_at"] = job["started_at"]
    _write_job(job)
    _deliver_update(job, _format_start(job))

    try:
        proc = subprocess.Popen(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_child_env(),
            bufsize=1,
        )
    except FileNotFoundError:
        job["status"] = "failed"
        job["error"] = f"Executable not found: {argv[0]}"
        job["elapsed_sec"] = round(time.time() - started, 1)
        job["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        _write_job(job)
        _deliver_update(job, _format_progress(job, {"event": "error", "message": job["error"]}) or job["error"])
        return

    job["pid"] = proc.pid
    _write_job(job)
    output_lines: list[str] = []
    deadline = started + int(job.get("timeout") or 1800)
    try:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if line:
                output_lines.append(line)
                _append_log(log_path, line)
                if line.startswith(_PROGRESS_PREFIX):
                    try:
                        payload = json.loads(line[len(_PROGRESS_PREFIX):])
                    except json.JSONDecodeError:
                        payload = {}
                    if payload:
                        if payload.get("output"):
                            job["output"] = payload["output"]
                        job["last_progress"] = payload
                        job["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
                        if _should_send_progress(job, payload):
                            message = _format_progress(job, payload)
                            if message:
                                _deliver_update(job, message)
                                _note_progress_sent(job, payload)
                        _write_job(job)
            if proc.poll() is not None:
                remainder = proc.stdout.read()
                if remainder:
                    output_lines.append(remainder)
                    _append_log(log_path, remainder)
                break
            if time.time() > deadline:
                proc.kill()
                raise TimeoutError(f"/{job['name']} timed out after {job.get('timeout')} seconds")
            time.sleep(0.1)

        rc = proc.returncode
        latest = _read_job(job["job_id"]) or job
        was_cancelled = latest.get("status") == "cancel_requested"
        job["exit_code"] = rc
        job["elapsed_sec"] = round(time.time() - started, 1)
        job["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        job["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        job.pop("pid", None)
        visible_output = "".join(
            line for line in output_lines if not line.startswith(_PROGRESS_PREFIX)
        ).strip()
        if was_cancelled:
            job["status"] = "cancelled"
            job["error"] = "Cancelled by request."
            _write_job(job)
            _deliver_update(job, f"/{job['name']} cancelled\nJob: {job['job_id']}")
        elif rc == 0:
            job["status"] = "complete"
            if visible_output:
                final_line = visible_output.splitlines()[-1].strip()
                if final_line:
                    job.setdefault("output", final_line)
            _write_job(job)
            _deliver_update(
                job,
                _format_progress(
                    job,
                    {
                        "event": "complete",
                        "elapsed_sec": job["elapsed_sec"],
                        "output": job.get("output"),
                    },
                )
                or f"/{job['name']} complete",
            )
        else:
            job["status"] = "failed"
            job["error"] = _trim_output(job["name"], visible_output, limit=1200)
            _write_job(job)
            _deliver_update(
                job,
                f"/{job['name']} failed\nJob: {job['job_id']}\nExit code: {rc}\n{job['error']}",
            )
    except Exception as exc:
        latest = _read_job(job["job_id"]) or job
        job["status"] = "cancelled" if latest.get("status") == "cancel_requested" else "failed"
        job["elapsed_sec"] = round(time.time() - started, 1)
        job["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        job["error"] = "Cancelled by request." if job["status"] == "cancelled" else _sanitize_text(str(exc))
        job["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        job.pop("pid", None)
        _write_job(job)
        _deliver_update(job, f"/{job['name']} {job['status']}\nJob: {job['job_id']}\n{job['error']}")


def _run(name: str, argv: list[str], timeout: int = 1800) -> str:
    try:
        proc = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=_child_env(),
            check=False,
        )
    except FileNotFoundError:
        return f"Executable not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return f"/{name} timed out after {timeout} seconds."

    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        output = f"Exit code: {proc.returncode}\n{output}"
    return _trim_output(name, output)


def _handle_vtt(raw_args: str) -> str:
    args = _split(raw_args)
    if not args:
        return "Usage: /vtt <audio-or-video-path> [--model small] [--language zh]"
    model = _parse_option(args, {"--model"}, "small")
    backend = _parse_option(args, {"--backend"}, "auto")
    device = _parse_option(args, {"--device"}, "auto")
    meta = {
        "model": model,
        "environment": f"local STT ({backend}, device {device})",
        "input": args[0],
    }
    return _start_background_job(
        "vtt",
        [sys.executable, str(BIN_DIR / "vtt_local_optimized.py"), *args],
        meta,
        timeout=7200,
    )


def _handle_ebook(raw_args: str) -> str:
    args = _split(raw_args)
    if not args:
        return "Usage: /ebook <ebook-or-text-path> [output.epub|output.mobi] [target language] [--format epub|mobi|azw3|both] [--style comfortable|compact]"
    env = _child_env()
    model = env.get("OLLAMA_EBOOK_MODEL", "qwen3:8b")
    base_url = env.get("OLLAMA_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    positional = _non_option_args(args)
    target = env.get("HERMES_EBOOK_TARGET_LANG", "Traditional Chinese")
    output_format = _parse_option(args, {"--format"}, None)
    style = _parse_option(args, {"--style"}, "comfortable")
    output = None
    if len(positional) >= 2:
        if _looks_like_output_path(positional[1]):
            output = positional[1]
            if len(positional) >= 3:
                target = positional[2]
        else:
            target = positional[1]
    if output_format is None and output:
        suffix = pathlib.Path(output).suffix.lower()
        output_format = suffix[1:] if suffix in {".epub", ".mobi", ".azw3"} else "epub"
    output_format = output_format or "epub"
    meta = {
        "model": model,
        "environment": f"local Ollama + optimized EPUB rebuild ({base_url})",
        "input": args[0],
        "target_language": target,
        "output_format": output_format,
        "style": style,
    }
    return _start_background_job(
        "ebook",
        [str(BIN_DIR / "ebook_translate_local.sh"), *args],
        meta,
        timeout=14400,
    )


def _handle_infographic(raw_args: str) -> str:
    args = _split(raw_args)
    return _run("infographic-last", [str(BIN_DIR / "infographic_glm.sh"), *args], timeout=3600)


def _handle_code(raw_args: str) -> str:
    prompt = (raw_args or "").strip()
    if not prompt:
        return "Usage: /code <coding or complex-work prompt>"

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    prompt_file = CACHE_DIR / f"code-glm-prompt-{stamp}.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    os.chmod(prompt_file, 0o600)

    hermes_bin = os.environ.get("HERMES_BIN", "hermes")
    task = (
        f"Read the task from {prompt_file}. Work as the code-glm worker "
        "using OpenRouter GLM 5.2. Keep secrets out of logs and responses. "
        "Return the result or the next concrete action."
    )
    return _run("code", [hermes_bin, "-p", "code-glm", "-z", task], timeout=3600)


def _handle_agency_job(raw_args: str) -> str:
    args = _split(raw_args)
    if not args:
        jobs = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:8] if JOBS_DIR.exists() else []
        if not jobs:
            return "No Hermes Agency jobs found yet."
        lines = ["Recent Hermes Agency jobs:"]
        for path in jobs:
            job = _read_job(path.stem) or {}
            lines.append(
                f"{job.get('job_id', path.stem)} | {job.get('name', '?')} | "
                f"{job.get('status', '?')} | {job.get('updated_at', job.get('created_at', ''))}"
            )
        return "\n".join(lines)

    job = _read_job(args[0])
    if not job:
        return f"Job not found: {args[0]}"
    meta = job.get("meta") or {}
    lines = [
        f"Job: {job.get('job_id')}",
        f"Command: /{job.get('name')}",
        f"Status: {job.get('status')}",
        f"Model: {meta.get('model', 'local')}",
        f"Environment: {meta.get('environment', 'local')}",
        f"Started: {job.get('started_at', job.get('created_at', ''))}",
    ]
    if job.get("elapsed_sec"):
        lines.append(f"Elapsed: {_fmt_seconds(job.get('elapsed_sec'))}")
    if job.get("queue") == "local-heavy":
        position = _queue_position(job.get("job_id", ""), job.get("target_key"))
        if position is not None:
            lines.append(f"Queue position: {position}")
        if job.get("batch_id"):
            lines.append(f"Batch: {job['batch_id']}")
    if job.get("output"):
        lines.append(f"Output: {job['output']}")
    if job.get("error"):
        lines.append(f"Error: {_trim_output('agency-job', str(job['error']), limit=1200)}")
    if job.get("log_path"):
        lines.append(f"Log: {job['log_path']}")
    return "\n".join(lines)


def _handle_agency_queue(raw_args: str) -> str:
    _ensure_queue_worker()
    jobs = _heavy_jobs(_UNFINISHED_STATUSES)
    state = "paused" if _queue_paused() else "active"
    if not jobs:
        return f"Local heavy queue is empty.\nState: {state}\nConcurrency: 1 job at a time"

    lines = [
        f"Local heavy queue: {state}",
        "Concurrency: 1 job at a time",
    ]
    for job in jobs:
        meta = job.get("meta") or {}
        status = job.get("status", "?")
        label = f"/{job.get('name', '?')} {job.get('job_id', '?')} | {status}"
        if status == "queued":
            position = _queue_position(job.get("job_id", ""), job.get("target_key"))
            if position is not None:
                label += f" | position {position}"
        if meta.get("input"):
            label += f" | {_safe_name(str(meta['input']))}"
        model = meta.get("model")
        if model:
            label += f" | {model}"
        lines.append(label)
    return "\n".join(lines)


def _handle_agency_cancel(raw_args: str) -> str:
    args = _split(raw_args)
    if not args:
        return "Usage: /agency_cancel <job-id>"

    job = _read_job(args[0])
    if not job:
        return f"Job not found: {args[0]}"
    if job.get("queue") != "local-heavy":
        return f"Job {args[0]} is not a local heavy queue job."

    status = job.get("status")
    if status == "queued":
        job["status"] = "cancelled"
        job["error"] = "Cancelled before start."
        job["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        job["finished_at"] = job["updated_at"]
        _write_job(job)
        _maybe_report_batch_complete(job)
        _ensure_queue_worker()
        return f"Cancelled queued job: {job['job_id']}"

    if status in {"running", "cancel_requested"}:
        if status == "cancel_requested":
            return f"Cancel already requested for running job: {job['job_id']}"
        job["status"] = "cancel_requested"
        job["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        _write_job(job)
        pid = job.get("pid")
        if _pid_alive(pid):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception as exc:
                return f"Cancel requested, but process signal failed: {_sanitize_text(str(exc))}"
            return f"Cancel requested for running job: {job['job_id']}"
        job["status"] = "cancelled"
        job["error"] = "Cancelled; process was not running."
        job["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        _write_job(job)
        _maybe_report_batch_complete(job)
        _ensure_queue_worker()
        return f"Cancelled stale running job: {job['job_id']}"

    return f"Job {job['job_id']} is already {status}."


def _handle_agency_pause(raw_args: str) -> str:
    _set_queue_paused(True)
    return "Local heavy queue paused. Current running job will continue; next jobs will wait."


def _handle_agency_resume(raw_args: str) -> str:
    _set_queue_paused(False)
    _ensure_queue_worker()
    return "Local heavy queue resumed."


def register(ctx) -> None:
    ctx.register_command(
        "vtt",
        handler=_handle_vtt,
        description="Local-only audio/video transcription to VTT.",
        args_hint="<audio-or-video-path>",
    )
    ctx.register_command(
        "ebook",
        handler=_handle_ebook,
        description="Local-only optimized EPUB/MOBI eBook translation with Ollama qwen3:8b.",
        args_hint="<ebook-or-text-path> [output.epub|output.mobi] [target language]",
    )
    ctx.register_command(
        "infographic-last",
        handler=_handle_infographic,
        description="Send latest transcript/session to code-glm for an infographic brief.",
        args_hint="[transcript-path]",
    )
    ctx.register_command(
        "code",
        handler=_handle_code,
        description="Delegate coding or complex work directly to code-glm.",
        args_hint="<prompt>",
    )
    ctx.register_command(
        "agency-job",
        handler=_handle_agency_job,
        description="Show Hermes Agency background job status.",
        args_hint="[job-id]",
    )
    ctx.register_command(
        "agency-queue",
        handler=_handle_agency_queue,
        description="Show the local heavy-task queue for /ebook and /vtt.",
        args_hint="",
    )
    ctx.register_command(
        "agency-cancel",
        handler=_handle_agency_cancel,
        description="Cancel a queued or running local heavy-task job.",
        args_hint="<job-id>",
    )
    ctx.register_command(
        "agency-pause",
        handler=_handle_agency_pause,
        description="Pause the local heavy-task queue after the current job.",
        args_hint="",
    )
    ctx.register_command(
        "agency-resume",
        handler=_handle_agency_resume,
        description="Resume the local heavy-task queue.",
        args_hint="",
    )
