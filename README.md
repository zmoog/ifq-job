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
- `OTEL_ENABLED` (`true`/`false`, default `false`)
- `OTEL_SERVICE_NAME` (default `ifq-job`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` (optional OTLP metrics endpoint)

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

- Structured JSON logs are always enabled.
- Metrics are emitted via OpenTelemetry when `OTEL_ENABLED=true`.
- See [docs/observability-spec.md](docs/observability-spec.md).
