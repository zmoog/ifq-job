#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import requests
from ifq import DownloadError, IssueNotAvailableError, LoginError, Scraper
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

FILENAME_PATTERN = "ilfatto-%Y%m%d.pdf"
SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "ifq-job")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def log_event(level: str, event: str, run_id: str, **fields: object) -> None:
    payload = {
        "timestamp": now_utc_iso(),
        "level": level,
        "service.name": SERVICE_NAME,
        "event": event,
        "run_id": run_id,
    }
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), file=sys.stdout, flush=True)


class Observability:
    def __init__(self):
        self.enabled = env_bool("OTEL_ENABLED", False)
        self.provider = None
        self.runs_total = None
        self.duration = None
        self.retries_total = None
        self.errors_total = None
        self.dropbox_exists_checks_total = None

        if not self.enabled:
            return

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        exporter = OTLPMetricExporter(endpoint=endpoint) if endpoint else OTLPMetricExporter()
        reader = PeriodicExportingMetricReader(exporter)

        resource = Resource.create({"service.name": SERVICE_NAME})
        self.provider = MeterProvider(metric_readers=[reader], resource=resource)
        metrics.set_meter_provider(self.provider)

        meter = metrics.get_meter(SERVICE_NAME)
        self.runs_total = meter.create_counter("ifq_job_runs_total")
        self.duration = meter.create_histogram("ifq_job_duration_seconds")
        self.retries_total = meter.create_counter("ifq_job_retries_total")
        self.errors_total = meter.create_counter("ifq_job_errors_total")
        self.dropbox_exists_checks_total = meter.create_counter(
            "ifq_dropbox_exists_checks_total"
        )

    def add_run(self, status: str, attrs: Dict[str, str]) -> None:
        if self.runs_total:
            self.runs_total.add(1, attributes={**attrs, "status": status})

    def record_duration(self, duration_seconds: float, attrs: Dict[str, str]) -> None:
        if self.duration:
            self.duration.record(duration_seconds, attributes=attrs)

    def add_retry(self, attrs: Dict[str, str]) -> None:
        if self.retries_total:
            self.retries_total.add(1, attributes=attrs)

    def add_error(self, bucket: str, error_type: str, attrs: Dict[str, str]) -> None:
        if self.errors_total:
            self.errors_total.add(
                1,
                attributes={
                    **attrs,
                    "bucket": bucket,
                    "error_type": error_type,
                },
            )

    def add_dropbox_exists_check(self, result: str, attrs: Dict[str, str]) -> None:
        if self.dropbox_exists_checks_total:
            self.dropbox_exists_checks_total.add(
                1,
                attributes={**attrs, "result": result},
            )

    def shutdown(self) -> None:
        if not self.provider:
            return
        self.provider.force_flush()
        self.provider.shutdown()


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def parse_day(raw: Optional[str]) -> date:
    if not raw:
        return date.today()
    return datetime.strptime(raw, "%Y-%m-%d").date()


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value < 1:
        raise RuntimeError(f"{name} must be >= 1, got {value}")
    return value


def run_ifq_download(username: str, password: str, day: date, output_dir: str) -> Path:
    scraper = Scraper(username=username, password=password)
    output_file = scraper.download_pdf(pub_date=day, output_dir=Path(output_dir))
    local_path = Path(output_file)
    if not local_path.exists():
        raise RuntimeError("IFQ download completed but PDF file was not found")
    return local_path


def dropbox_issue_exists(token: str, root: str, filename: str) -> bool:
    response = requests.post(
        "https://api.dropboxapi.com/2/files/search_v2",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "query": filename,
            "options": {
                "path": root,
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return len(payload.get("matches", [])) > 0


def upload_to_dropbox(
    token: str,
    root: str,
    local_path: Path,
    remote_filename: str,
) -> None:
    with local_path.open("rb") as f:
        response = requests.post(
            "https://content.dropboxapi.com/2/files/upload",
            headers={
                "Authorization": f"Bearer {token}",
                "Dropbox-API-Arg": json.dumps(
                    {
                        "path": f"{root}/{remote_filename}",
                        "mode": "add",
                        "autorename": False,
                        "mute": True,
                    }
                ),
                "Content-Type": "application/octet-stream",
            },
            data=f.read(),
            timeout=60,
        )
        response.raise_for_status()


def is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, IssueNotAvailableError):
        return True

    if isinstance(exc, (LoginError, DownloadError, IndexError, ValueError)):
        return False

    if isinstance(exc, requests.exceptions.SSLError):
        msg = str(exc).lower()
        if "certificate verify failed" in msg or "cert" in msg and "expired" in msg:
            return False
        return True

    if isinstance(
        exc,
        (requests.exceptions.Timeout, requests.exceptions.ConnectionError),
    ):
        return True

    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status in (408, 425, 429, 500, 502, 503, 504)

    if isinstance(exc, requests.exceptions.RequestException):
        return True

    return False


def run_once(
    ifq_username: str,
    ifq_password: str,
    dropbox_token: str,
    dropbox_root: str,
    requested_day: date,
    run_id: str,
    obs: Observability,
    attrs: Dict[str, str],
) -> None:
    filename = requested_day.strftime(FILENAME_PATTERN)

    log_event(
        "INFO",
        "dropbox_exists_check_start",
        run_id,
        filename=filename,
        dropbox_root=dropbox_root,
    )
    if dropbox_issue_exists(dropbox_token, dropbox_root, filename):
        obs.add_dropbox_exists_check("exists", attrs)
        log_event("INFO", "dropbox_exists", run_id, filename=filename)
        return

    obs.add_dropbox_exists_check("missing", attrs)
    log_event("INFO", "ifq_download_start", run_id, issue_date=requested_day.isoformat())

    with tempfile.TemporaryDirectory() as tmpdir:
        local_pdf = run_ifq_download(
            ifq_username,
            ifq_password,
            requested_day,
            tmpdir,
        )

        log_event("INFO", "dropbox_upload_start", run_id, filename=filename)
        upload_to_dropbox(dropbox_token, dropbox_root, local_pdf, filename)

    log_event("INFO", "run_once_done", run_id, filename=filename)


def main() -> int:
    run_id = str(uuid.uuid4())
    obs = Observability()
    start = time.monotonic()
    status = "failed"
    attrs: Dict[str, str] = {}

    try:
        ifq_username = env("IFQ_USERNAME")
        ifq_password = env("IFQ_PASSWORD")
        dropbox_token = env("DROPBOX_ACCESS_TOKEN")
        dropbox_root = env("DROPBOX_ROOT_FOLDER")
        requested_day = parse_day(os.environ.get("IFQ_DAY"))

        attempts = env_int("IFQ_RETRY_ATTEMPTS", 3)
        delay_seconds = env_int("IFQ_RETRY_DELAY_SECONDS", 60)

        attrs = {
            "issue_date": requested_day.isoformat(),
        }

        log_event(
            "INFO",
            "job_start",
            run_id,
            issue_date=requested_day.isoformat(),
            max_attempts=attempts,
        )

        for attempt in range(1, attempts + 1):
            try:
                log_event("INFO", "attempt_start", run_id, attempt=attempt)
                run_once(
                    ifq_username,
                    ifq_password,
                    dropbox_token,
                    dropbox_root,
                    requested_day,
                    run_id,
                    obs,
                    attrs,
                )
                status = "success"
                log_event("INFO", "job_success", run_id, attempt=attempt)
                return 0
            except Exception as exc:
                transient = is_transient_error(exc)
                bucket = "transient" if transient else "persistent"
                error_type = type(exc).__name__
                obs.add_error(bucket, error_type, attrs)

                log_event(
                    "ERROR",
                    "attempt_failed",
                    run_id,
                    attempt=attempt,
                    max_attempts=attempts,
                    bucket=bucket,
                    error_type=error_type,
                    error=str(exc),
                )

                if (not transient) or attempt == attempts:
                    return 1

                obs.add_retry(attrs)
                log_event(
                    "INFO",
                    "retry_scheduled",
                    run_id,
                    next_attempt=attempt + 1,
                    delay_seconds=delay_seconds,
                )
                time.sleep(delay_seconds)

        return 1
    except Exception as exc:
        log_event(
            "ERROR",
            "job_failed_before_attempts",
            run_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return 1
    finally:
        duration = time.monotonic() - start
        obs.add_run(status, attrs)
        obs.record_duration(duration, attrs)
        log_event("INFO", "job_end", run_id, status=status, duration_seconds=duration)
        obs.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
