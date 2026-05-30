# ifq-job

Minimal job that:
1. downloads IFQ PDF for a day (default: today) using `ifq`
2. uploads it to Dropbox

## Required env vars

- `IFQ_USERNAME`
- `IFQ_PASSWORD`
- `DROPBOX_ACCESS_TOKEN`
- `DROPBOX_ROOT_FOLDER`

## Optional env vars

- `IFQ_DAY` (format `YYYY-MM-DD`)
- `IFQ_RETRY_ATTEMPTS` (default `3`)
- `IFQ_RETRY_DELAY_SECONDS` (default `60`)

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python ifq_job.py
```

## Build image

```bash
docker build -t ghcr.io/zmoog/ifq-job:latest .
```

## Observability

See [docs/observability-spec.md](docs/observability-spec.md).
