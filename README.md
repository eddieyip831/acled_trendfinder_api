# ACLED Trendfinder API

Serverless read only API that queries a Digital Ocean MySQL database from AWS Lambda, fronted by Amazon API Gateway and Amazon CloudFront.
The handler validates requests against an OpenAPI contract, supports a safe debug mode, emits structured logs, and publishes CloudWatch Embedded Metrics for timings and percentiles.

## Contents

1. Overview
2. Naming and versioning
3. System architecture diagrams
4. OpenAPI contract and validation
5. CloudWatch Embedded Metrics and where to place your SQL
6. Local development
7. Configuration and secrets
8. Debug mode
9. Unit tests with pytest
10. Packaging and deployment with AWS SAM
11. Packaging and deployment with Serverless Framework
12. Operations and troubleshooting

---

## 1. Overview

Endpoint
- Base path: `/v1/trendfinder`
- Method: `GET`
- Auth: API key header `X-Api-Key` or mutual TLS at the gateway if enabled
- Data store: Digital Ocean Managed MySQL with a private address and a read only user
- Transport: site to site VPN between AWS VPC and Digital Ocean VPC

Response shape
- A `meta` object for paging and a `data` array of events

Example request
```bash
curl -sS "https://your-id.execute-api.your-region.amazonaws.com/v1/trendfinder?country=Kenya&start_date=2025-01-01&end_date=2025-02-01&page=1&page_size=5"   -H "X-Api-Key: YOUR_API_KEY"   -H "X-Correlation-Id: demo-123"
```

---

## 2. Naming and versioning

- Use a clear version prefix such as `/v1`. Breaking changes go to `/v2`.
- Keep resource names short and lower case. We use `trendfinder` rather than `trend_finder`.
- For internal facing usage, prefer a private hostname or a dedicated internal domain, for example `internal.api.example.org/v1/trendfinder`, and restrict access with allow lists, private CloudFront, or mutual TLS.

Update `openapi.yaml` and the API Gateway route so they both use `/v1/trendfinder`.

---

## 3. System architecture diagrams

Add the rendered PNGs to a `docs` folder in this repository, then embed with Markdown.

```bash
mkdir -p docs
# copy your generated images into docs/
# docs/request_flow_app_team.png
# docs/acled_api_platform_vpc.png
```

Embed them in this README:

```markdown
### Request flow for the application team
![Request flow](docs/request_flow_app_team.png)

### Platform view for cloud engineering
![Platform view](docs/acled_api_platform_vpc.png)
```

---

## 4. OpenAPI contract and validation

- The contract is stored at the repository root as `openapi.yaml` and must define `/v1/trendfinder`.
- The Lambda uses a Pydantic model that mirrors the spec and rejects bad input with HTTP 400 before any database work.
- Required fields include `country`, `start_date`, `end_date`. Paging and sorting have limits and enums.

---

## 5. CloudWatch Embedded Metrics and where to place your SQL

Purpose
- Emit request count, validation time, query time, total time and rows returned. This gives p50 p90 p99 in CloudWatch without extra tools.

Where your SQL lives
- In `handler.py` the section marked **plan and execute query** is the template location for your SQL. Keep your query there. The wrapper around it measures timings and publishes metrics, then returns the normal response.

Install
Add to `requirements.txt` and install:
```
aws-embedded-metrics==3.5.0
```
```
pip install -r requirements.txt
```

Charting
- Open the `ACLED/Trendfinder` namespace in CloudWatch Metrics and view `QueryMs` and `TotalMs` with p95 or p99. Consider an alarm on p95 above your target and another alarm on `ErrorCount` per period.

---

## 6. Local development

Create and activate the virtual environment.
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Smoke test:
```bash
python handler.py
```

Deactivate and reactivate when needed.
```bash
deactivate
source .venv/bin/activate
```

---

## 7. Configuration and secrets

Local runs can read a `config.ini`. In AWS use environment variables and Secrets Manager. Never commit real secrets.

Example `config.ini`:
```ini
[DEFAULT]
DB_HOST = 10.0.10.25
DB_PORT = 3306
DB_USER = acled_readonly
DB_PASSWORD = change-me
DB_NAME = acled
DB_SSL_CA_PATH = /path/to/ca.pem

TABLE_NAME = events
DATE_FIELD = event_date

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

DEBUG_KEYS = DEBUG-LOCAL-001, DEBUG-LOCAL-002
LOG_LEVEL = INFO
VERBOSE = false
```

---

## 8. Debug mode

- Provide `X-Debug-Key` with one of the configured keys to enable richer diagnostics for a single request.
- To view detailed logs locally, set `LOG_LEVEL=DEBUG` in `config.ini` and restart the process.
- In debug mode the response includes a `meta.debug` block and the number of returned rows is capped to keep payloads small.

---

## 9. Unit tests with pytest

Install and run:
```bash
pip install -r requirements.txt
pytest -q
```
Tests in `tests` cover validation failures, success envelope, debug behaviour and CORS headers. `pytest.ini` at the project root keeps settings tidy.

---

## 10. Packaging and deployment with AWS SAM

Templates
- The file `template.yaml` is at the repository root.

Prerequisites
- AWS CLI and SAM CLI configured
- Docker if you prefer container based builds

Commands
```bash
sam build
sam deploy --guided
```

Output
- SAM prints the invoke URL. Test with the curl example above.
- Replace the security group and subnet ids and restrict the Secrets Manager permission to the specific secret ARN.

---

## 11. Packaging and deployment with Serverless Framework

Templates
- The file `serverless.yml` is at the repository root.

Prerequisites
- Node and npm
- Serverless Framework

Commands
```bash
npm i
sls deploy
```

Output
- Serverless prints the HTTP API endpoint. Test with the same curl example.

---

## 12. Operations and troubleshooting

- Use `X-Correlation-Id` to trace a request through logs and metrics.
- Validation errors return HTTP 400 with field details.
- Unexpected errors return HTTP 500 with a correlation id.
- Use the embedded metrics namespace to chart p95 query timings and rows returned.
- Keep CloudWatch log retention to a sensible period such as thirty days.

---

## Continuous scanning in GitHub Actions

A workflow lives at `.github/workflows/ci.yml`. It lints, tests and scans each push and pull request.
It runs Ruff, Bandit, pytest, pip audit and Semgrep with OWASP Top Ten rules.

## API specific dynamic testing

Start the API locally with SAM then run Schemathesis against your OpenAPI file.

```bash
sam local start-api
pip install schemathesis
schemathesis run --checks all --stateful=links http://127.0.0.1:3000/openapi.yaml
```
