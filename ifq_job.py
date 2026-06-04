#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

import requests
from ifq import DownloadError, IssueNotAvailableError, LoginError, Scraper
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

FILENAME_PATTERN = "ilfatto-%Y%m%d.pdf"
SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "ifq-job")


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value < 1:
        raise RuntimeError(f"{name} must be >= 1, got {value}")
    return value


def parse_day(raw: Optional[str]) -> date:
    if not raw:
        return date.today()
    return datetime.strptime(raw, "%Y-%m-%d").date()


def parse_otlp_headers(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {}
    headers = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        if sep:
            headers[key.strip()] = value.strip()
    return headers


def log(message: str) -> None:
    print(f"[ifq-job] {message}", flush=True)


class Tracing:
    def __init__(self):
        self.enabled = env_bool("OTEL_ENABLED", False)
        self.provider = None

        if self.enabled:
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            headers = parse_otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"))

            exporter_kwargs = {}
            if endpoint:
                exporter_kwargs["endpoint"] = endpoint
            if headers:
                exporter_kwargs["headers"] = headers

            exporter = OTLPSpanExporter(**exporter_kwargs)
            span_processor = BatchSpanProcessor(exporter)
            self.provider = TracerProvider(
                resource=Resource.create({"service.name": SERVICE_NAME})
            )
            self.provider.add_span_processor(span_processor)
            trace.set_tracer_provider(self.provider)

        self.tracer = trace.get_tracer(SERVICE_NAME)

    def shutdown(self) -> None:
        if not self.provider:
            return
        self.provider.force_flush()
        self.provider.shutdown()


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
    tracer,
    run_id: str,
    issue_date: str,
    ifq_username: str,
    ifq_password: str,
    dropbox_token: str,
    dropbox_root: str,
    requested_day: date,
) -> None:
    filename = requested_day.strftime(FILENAME_PATTERN)

    with tracer.start_as_current_span("dropbox.exists_check") as span:
        span.set_attribute("run_id", run_id)
        span.set_attribute("issue_date", issue_date)
        span.set_attribute("file.name", filename)
        span.set_attribute("dropbox.root", dropbox_root)

        exists = dropbox_issue_exists(dropbox_token, dropbox_root, filename)
        span.set_attribute("dropbox.exists", exists)
        if exists:
            log(f"{filename} already exists, nothing to do")
            return

    with tempfile.TemporaryDirectory() as tmpdir:
        with tracer.start_as_current_span("ifq.download") as span:
            span.set_attribute("run_id", run_id)
            span.set_attribute("issue_date", issue_date)
            local_pdf = run_ifq_download(
                ifq_username,
                ifq_password,
                requested_day,
                tmpdir,
            )

        upload_size = local_pdf.stat().st_size
        with tracer.start_as_current_span("dropbox.upload") as span:
            span.set_attribute("run_id", run_id)
            span.set_attribute("issue_date", issue_date)
            span.set_attribute("file.name", filename)
            span.set_attribute("file.size", upload_size)
            upload_to_dropbox(dropbox_token, dropbox_root, local_pdf, filename)

    log(f"done: {filename}")


def main() -> int:
    tracing = Tracing()
    run_id = str(uuid.uuid4())

    try:
        ifq_username = env("IFQ_USERNAME")
        ifq_password = env("IFQ_PASSWORD")
        dropbox_token = env("DROPBOX_ACCESS_TOKEN")
        dropbox_root = env("DROPBOX_ROOT_FOLDER")
        requested_day = parse_day(os.environ.get("IFQ_DAY"))

        attempts = env_int("IFQ_RETRY_ATTEMPTS", 3)
        delay_seconds = env_int("IFQ_RETRY_DELAY_SECONDS", 60)
        issue_date = requested_day.isoformat()

        log(f"start run_id={run_id} issue_date={issue_date} max_attempts={attempts}")

        with tracing.tracer.start_as_current_span("ifq.run") as root_span:
            root_span.set_attribute("run_id", run_id)
            root_span.set_attribute("issue_date", issue_date)
            root_span.set_attribute("max_attempts", attempts)

            for attempt in range(1, attempts + 1):
                with tracing.tracer.start_as_current_span("ifq.attempt") as attempt_span:
                    attempt_span.set_attribute("run_id", run_id)
                    attempt_span.set_attribute("issue_date", issue_date)
                    attempt_span.set_attribute("attempt", attempt)

                    try:
                        run_once(
                            tracing.tracer,
                            run_id,
                            issue_date,
                            ifq_username,
                            ifq_password,
                            dropbox_token,
                            dropbox_root,
                            requested_day,
                        )
                        root_span.set_attribute("attempt_count", attempt)
                        root_span.set_attribute("final_status", "success")
                        log(f"success run_id={run_id} attempt={attempt}")
                        return 0
                    except Exception as exc:
                        transient = is_transient_error(exc)
                        bucket = "transient" if transient else "persistent"

                        attempt_span.record_exception(exc)
                        attempt_span.set_attribute("error.type", type(exc).__name__)
                        attempt_span.set_attribute("error.bucket", bucket)
                        attempt_span.set_status(Status(StatusCode.ERROR, str(exc)))

                        if (not transient) or attempt == attempts:
                            root_span.record_exception(exc)
                            root_span.set_attribute("attempt_count", attempt)
                            root_span.set_attribute("final_status", "failed")
                            root_span.set_attribute("error.type", type(exc).__name__)
                            root_span.set_attribute("error.bucket", bucket)
                            root_span.set_status(Status(StatusCode.ERROR, str(exc)))
                            log(
                                f"failed run_id={run_id} attempt={attempt}/{attempts} bucket={bucket} error={type(exc).__name__}: {exc}"
                            )
                            return 1

                        log(
                            f"retrying run_id={run_id} attempt={attempt}/{attempts} in {delay_seconds}s bucket={bucket}"
                        )
                        with tracing.tracer.start_as_current_span("retry.sleep") as retry_span:
                            retry_span.set_attribute("run_id", run_id)
                            retry_span.set_attribute("issue_date", issue_date)
                            retry_span.set_attribute("attempt", attempt)
                            retry_span.set_attribute("delay_seconds", delay_seconds)
                            retry_span.set_attribute("error.type", type(exc).__name__)
                            retry_span.set_attribute("error.bucket", bucket)
                            time.sleep(delay_seconds)

            return 1
    except Exception as exc:
        log(f"failed run_id={run_id} before attempts error={type(exc).__name__}: {exc}")
        return 1
    finally:
        tracing.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
