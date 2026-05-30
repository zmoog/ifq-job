#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from ifq import DownloadError, IssueNotAvailableError, LoginError, Scraper

FILENAME_PATTERN = "ilfatto-%Y%m%d.pdf"


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
) -> None:
    filename = requested_day.strftime(FILENAME_PATTERN)

    print(f"[ifq-job] checking if {filename} already exists in {dropbox_root}")
    if dropbox_issue_exists(dropbox_token, dropbox_root, filename):
        print(f"[ifq-job] {filename} already exists, nothing to do")
        return

    print(f"[ifq-job] downloading IFQ issue for {requested_day.isoformat()}")
    with tempfile.TemporaryDirectory() as tmpdir:
        local_pdf = run_ifq_download(
            ifq_username,
            ifq_password,
            requested_day,
            tmpdir,
        )

        print(f"[ifq-job] uploading {filename} to Dropbox")
        upload_to_dropbox(dropbox_token, dropbox_root, local_pdf, filename)

    print(f"[ifq-job] done: {filename}")


def main() -> int:
    ifq_username = env("IFQ_USERNAME")
    ifq_password = env("IFQ_PASSWORD")
    dropbox_token = env("DROPBOX_ACCESS_TOKEN")
    dropbox_root = env("DROPBOX_ROOT_FOLDER")
    requested_day = parse_day(os.environ.get("IFQ_DAY"))

    attempts = env_int("IFQ_RETRY_ATTEMPTS", 3)
    delay_seconds = env_int("IFQ_RETRY_DELAY_SECONDS", 60)

    for attempt in range(1, attempts + 1):
        try:
            run_once(
                ifq_username,
                ifq_password,
                dropbox_token,
                dropbox_root,
                requested_day,
            )
            return 0
        except Exception as exc:
            transient = is_transient_error(exc)
            bucket = "transient" if transient else "persistent"
            print(
                f"[ifq-job] attempt {attempt}/{attempts} failed ({bucket}): {exc!r}",
                file=sys.stderr,
            )

            if (not transient) or attempt == attempts:
                return 1

            print(f"[ifq-job] retrying in {delay_seconds}s")
            time.sleep(delay_seconds)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
