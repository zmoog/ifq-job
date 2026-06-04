# IFQ Job Observability Spec

## Goal
Detect and diagnose job failures quickly, and understand run duration and retry behavior.

## Scope
Applies to `ifq_job.py` runs in Kubernetes `Job`/`CronJob`.

## Telemetry

### Metrics (required)
- `ifq_job_runs_total{status="success|failed"}`
- `ifq_job_duration_seconds` (histogram)
- `ifq_job_retries_total`
- `ifq_job_errors_total{bucket="transient|persistent",error_type="..."}`
- `ifq_dropbox_exists_checks_total{result="exists|missing"}`

### Logs (required)
Structured JSON logs with:
- `service.name=ifq-job`
- `run_id` (uuid per execution)
- `issue_date`
- `attempt`
- `event` (`start`, `retry`, `download_done`, `upload_done`, `failed`, ...)
- `error_type`, `error_bucket` (on failures)

### Traces (phase 2)
Spans:
- `ifq.run` (root)
- `ifq.dropbox.exists`
- `ifq.download`
- `ifq.dropbox.upload`

Span attributes:
- `run_id`, `issue_date`, `attempt`, `retry_count`, `result`
- `file.name` on file-related spans (PDF filename)
- `file.size` on `ifq.dropbox.upload` (uploaded PDF size in bytes)

## Error classification
Follow current app buckets:
- **Transient**: network timeouts/connection errors, retryable HTTP, temporary availability
- **Persistent**: bad credentials, invalid response/content, cert validation failures, parsing breakages

## OTel instrumentation requirements
- Use OpenTelemetry SDK + OTLP exporter
- Support enabling/disabling via env var
- Flush before process exit (`force_flush`/`shutdown`) because job is short-lived
- Never emit secrets or tokens in telemetry

### Suggested env vars
- `OTEL_ENABLED=true|false`
- `OTEL_SERVICE_NAME=ifq-job`
- `OTEL_EXPORTER_OTLP_ENDPOINT`
- `OTEL_EXPORTER_OTLP_HEADERS` (if auth is required)
- `OTEL_RESOURCE_ATTRIBUTES` (e.g. `deployment.environment=dev`)

## Backend requirements
Backend must provide:
- OTLP ingest
- metrics query + alerting
- structured log search
- (phase 2) trace search
- retention >= 30 days

Recommended path:
`ifq-job -> OTel Collector -> backend`

## Alerting requirements
- Failed job run
- No successful run in 24h
- Runtime above threshold (e.g. p95 > 5m)
- Retry spike

## Dashboards
Minimum panels:
- success/failure counts (24h/7d)
- duration p50/p95
- retries over time
- errors by type/bucket
- last successful run timestamp

## Rollout plan
1. Add structured logs + metrics
2. Validate in Kind/dev
3. Add alerts + dashboard
4. Add traces (phase 2)

## Acceptance criteria
- A failed run triggers alert within 5 minutes
- Easy answers to:
  - Did it run today?
  - Why did it fail?
  - How long did it take?
  - Which step was slow?
