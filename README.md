# ACLED Trendfinder API

Serverless read only API that queries a Digital Ocean MySQL database from AWS Lambda, fronted by Amazon API Gateway and Amazon CloudFront.
The handler enforces an OpenAPI style contract with Pydantic validation, supports a safe debug mode, emits structured logs, and publishes CloudWatch Embedded Metrics for timings and percentiles.
This work aligns with ACLED enterprise architecture practice and project governance using TOGAF and PRINCE2.

## Contents

1. Overview
2. Architecture
3. OpenAPI contract and validation
4. CloudWatch Embedded Metrics for timings and percentiles
5. Local development
6. Configuration and secrets
7. Debug mode
8. Unit tests with pytest
9. Packaging and deployment with AWS SAM
10. Packaging and deployment with Serverless Framework
11. Security and networking notes
12. Operations and troubleshooting
13. Appendix templates

## 1. Overview

Trendfinder is a read only endpoint.

- Path: `/trendfinder`
- Method: `GET`
- Auth: API key header `X-Api-Key` or mutual TLS at the gateway when enabled
- Validation: Pydantic in the Lambda handler that mirrors `openapi.yaml`
- Data store: Digital Ocean Managed MySQL with a private address and a read only user
- Transport: site to site VPN between AWS VPC and Digital Ocean VPC

The response contains a `meta` object for paging and a `data` array of events.

## 2. Architecture

Application team flow.

1. The application on a Digital Ocean Droplet calls CloudFront with `X-Api-Key` and optional `X-Correlation-Id`.
2. CloudFront forwards to Amazon API Gateway using the HTTP API type.
3. API Gateway invokes the Lambda handler inside an AWS VPC.
4. The handler validates the request and returns HTTP 400 early when input is not valid.
5. The handler retrieves secrets, connects over the VPN, and runs a read only query.
6. The handler writes logs to CloudWatch and returns a paged result.

A platform view adds VPCs on both sides, site to site VPN, Secrets Manager, and CloudWatch.

## 3. OpenAPI contract and validation

- The contract is stored in `openapi.yaml`.
- The Lambda uses Pydantic models to validate query parameters before any database work.
- Invalid requests return HTTP 400 with a list of field errors.
- The schema mirrors the OpenAPI file so code and contract stay aligned.

Main fields.

- `country` required
- `start_date` and `end_date` required
- `page` and `page_size` with limits
- `sort_by` and `sort_dir` with allowed values

## 4. CloudWatch Embedded Metrics for timings and percentiles

This adds first class service timings without a separate metrics agent. CloudWatch detects metrics in your logs and you can chart p50 p90 p99 directly.

### 4.1 Install the library

Add to `requirements.txt` then install in your venv.

```
aws-embedded-metrics==3.5.0
```

```
pip install -r requirements.txt
```

### 4.2 Instrument the handler

Add the imports near the top of `handler.py`.

```python
import time
from aws_embedded_metrics import metric_scope
```

Wrap your existing logic with this structure. The inner function returns your normal response and emits metrics.

```python
def handler(event, context):

    @metric_scope
    def _run(metrics):
        start_total = time.perf_counter()

        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        corr_id = headers.get("x-correlation-id") or event.get("requestContext", {}).get("requestId")
        path = event.get("path") or "/trendfinder"

        metrics.set_namespace("ACLED/Trendfinder")
        metrics.put_dimensions({
            "Service": "Trendfinder",
            "Function": os.getenv("AWS_LAMBDA_FUNCTION_NAME", "local"),
            "ApiPath": path
        })
        metrics.put_metric("RequestCount", 1, "Count")
        metrics.set_property("CorrelationId", corr_id)
        metrics.set_property("Path", path)

        # validate request
        t0 = time.perf_counter()
        params = parse_params(event)
        simple = {k: (v[0] if isinstance(v, list) and v else v) for k, v in params.items()}
        try:
            q = TrendfinderQuery(**simple)
            metrics.put_metric("ValidationMs", (time.perf_counter() - t0) * 1000.0, "Milliseconds")
        except ValidationError as ve:
            metrics.put_metric("ValidationMs", (time.perf_counter() - t0) * 1000.0, "Milliseconds")
            metrics.put_dimensions({"Outcome": "ValidationError"})
            metrics.put_metric("ErrorCount", 1, "Count")
            body = {
                "error": "bad_request",
                "message": "Request did not match the API contract",
                "details": ve.errors(),
                "correlation_id": corr_id,
            }
            return response(400, body, correlation_id=corr_id)

        # plan and execute query
        t1 = time.perf_counter()
        page = int(q.page)
        page_size = min(int(q.page_size), MAX_PAGE_SIZE)
        offset = (page - 1) * page_size

        sql_params = []
        where_sql = build_filters_from_validated(q, sql_params)
        order_sql = build_sort_from_validated(q)

        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                count_sql = f"SELECT COUNT(1) AS total FROM {TABLE_NAME}{where_sql}"
                select_sql = (
                    f"SELECT event_id, {DATE_FIELD} AS event_date, country, admin1, "
                    f"event_type, sub_event_type, actor1, actor2, fatalities, latitude, longitude "
                    f"FROM {TABLE_NAME}{where_sql}{order_sql} LIMIT %s OFFSET %s"
                )
                cur.execute(count_sql, sql_params)
                total = cur.fetchone()["total"]
                cur.execute(select_sql, sql_params + [page_size, offset])
                rows = cur.fetchall()
        except Exception:
            metrics.put_dimensions({"Outcome": "QueryError"})
            metrics.put_metric("ErrorCount", 1, "Count")
            raise

        metrics.put_metric("QueryMs", (time.perf_counter() - t1) * 1000.0, "Milliseconds")

        debug_on = is_debug(event, params)
        if debug_on:
            rows = rows[:int(os.getenv("DEBUG_MAX_ROWS", "50"))]

        total_pages = (total + page_size - 1) // page_size
        body = {
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "sort": {"by": q.sort_by, "dir": q.sort_dir},
                "filters": simple,
                "correlation_id": corr_id,
                "debug": {"validation": "ok"} if debug_on else None,
            },
            "data": rows,
        }

        metrics.put_metric("RowsReturned", len(rows), "Count")
        metrics.put_metric("TotalMs", (time.perf_counter() - start_total) * 1000.0, "Milliseconds")
        metrics.put_dimensions({"Outcome": "OK"})

        return response(200, body, correlation_id=corr_id)

    try:
        return _run()
    except Exception as ex:
        log_json(logging.ERROR, msg="unhandled_exception", error=str(ex))
        return response(
            500,
            {"error": "internal_error", "message": "An unexpected error occurred"},
        )
```

### 4.3 Chart percentiles and alarms

In CloudWatch Metrics, open the namespace `ACLED/Trendfinder`. Select `QueryMs` and `TotalMs`. Use the statistics drop down to view p50 p90 p99. Create alarms for p95 or p99 based on your performance target. Add another alarm on `ErrorCount` when it exceeds a small threshold per period.

## 5. Local development

Create and activate a virtual environment.

```
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Quick smoke test.

```
python handler.py
```

Deactivate and reactivate when needed.

```
deactivate
source .venv/bin/activate
```

## 6. Configuration and secrets

Local runs can read a plain text `config.ini`. In AWS use environment variables and Secrets Manager. Never commit real secrets.

Example `config.ini`.

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

## 7. Debug mode

- Provide `X-Debug-Key` with one of the configured keys to enable richer diagnostics for a single request.
- The response includes `meta.debug`.
- The number of returned rows is capped to keep payloads small.

## 8. Unit tests with pytest

Install and run.

```
pip install -r requirements.txt
pytest -q
```

Tests are in `tests` and cover validation failures, success envelope, debug behaviour, and CORS headers. A `pytest.ini` at the project root keeps settings tidy.

## 9. Packaging and deployment with AWS SAM

### 9.1 Prerequisites

- AWS CLI and SAM CLI configured
- Docker available if you want container based builds

Check versions.

```
aws --version
sam --version
```

### 9.2 Template file

Create `template.yaml` at the repository root.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Description: ACLED Trendfinder API

Globals:
  Function:
    Runtime: python3.12
    Timeout: 30
    MemorySize: 512
    Tracing: Active
    Environment:
      Variables:
        TABLE_NAME: events
        DATE_FIELD: event_date
        DEFAULT_PAGE_SIZE: 50
        MAX_PAGE_SIZE: 500
        LOG_LEVEL: INFO
        VERBOSE: false

Resources:
  TrendfinderFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: handler.handler
      CodeUri: .
      Description: Trendfinder read only query
      Policies:
        - AWSLambdaVPCAccessExecutionRole
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: secretsmanager:GetSecretValue
              Resource: "*" # restrict to the exact secret ARN in production
      Events:
        HttpGet:
          Type: HttpApi
          Properties:
            Path: /trendfinder
            Method: GET
      VpcConfig:
        SecurityGroupIds:
          - sg-xxxxxxxx
        SubnetIds:
          - subnet-aaaaaaa
          - subnet-bbbbbbb

  HttpApi:
    Type: AWS::Serverless::HttpApi
    Properties:
      CorsConfiguration:
        AllowOrigins: ["*"]
        AllowMethods: ["GET", "OPTIONS"]
        AllowHeaders:
          - Content-Type
          - Authorization
          - X-Debug-Key
          - X-Correlation-Id

Outputs:
  ApiUrl:
    Value: !Sub "https://${HttpApi}.execute-api.${AWS::Region}.amazonaws.com/trendfinder"
    Description: HTTP API invoke URL
```

### 9.3 Build and deploy

```
sam build
sam deploy --guided
```

SAM prints the API endpoint.

Test with curl.

```
curl -sS   -H "X-Api-Key: YOUR_API_KEY"   "https://your-id.execute-api.your-region.amazonaws.com/trendfinder?country=Kenya&start_date=2025-01-01&end_date=2025-02-01&page=1&page_size=5"
```

Logs.

```
aws logs tail /aws/lambda/YourFunctionName --follow
```

## 10. Packaging and deployment with Serverless Framework

### 10.1 Prerequisites

- Node and npm installed
- Serverless Framework installed

Install.

```
npm install -g serverless
sls --version
```

### 10.2 Configuration file

Create `serverless.yml` at the repository root.

```yaml
service: acled-trendfinder-api
frameworkVersion: '3'

provider:
  name: aws
  runtime: python3.12
  region: eu-west-2
  timeout: 30
  memorySize: 512
  httpApi:
    cors:
      allowedOrigins: ['*']
      allowedMethods: ['GET', 'OPTIONS']
      allowedHeaders:
        - Content-Type
        - Authorization
        - X-Debug-Key
        - X-Correlation-Id
  environment:
    TABLE_NAME: events
    DATE_FIELD: event_date
    DEFAULT_PAGE_SIZE: 50
    MAX_PAGE_SIZE: 500
    LOG_LEVEL: INFO
    VERBOSE: false
  iam:
    role:
      statements:
        - Effect: Allow
          Action:
            - secretsmanager:GetSecretValue
          Resource: '*' # restrict in production
        - Effect: Allow
          Action:
            - logs:CreateLogGroup
            - logs:CreateLogStream
            - logs:PutLogEvents
          Resource: '*'
  vpc:
    securityGroupIds:
      - sg-xxxxxxxx
    subnetIds:
      - subnet-aaaaaaa
      - subnet-bbbbbbb

functions:
  trendfinder:
    handler: handler.handler
    events:
      - httpApi:
          path: /trendfinder
          method: get

plugins:
  - serverless-python-requirements

custom:
  pythonRequirements:
    dockerizePip: true
    slim: true
```

### 10.3 Deploy

```
npm i
sls deploy
```

Serverless prints the HTTP API endpoint. Test with the same curl example.

## 11. Security and networking notes

- API Gateway HTTP API supports JWT and Lambda authorisers. For a single client an API key is sufficient. For stronger identity enable mutual TLS on a dedicated custom domain.
- CloudFront is in front of API Gateway for performance, cache control, and WAF integration.
- Lambda reaches the database only through the site to site VPN.
- Use a least privilege read only database user.
- Keep TLS on every hop.
- A NAT gateway is not required for the core path. If the function needs general internet access add NAT later and document the egress rules.

## 12. Operations and troubleshooting

- Use `X-Correlation-Id` to trace a request across logs and metrics.
- Validation errors return HTTP 400 with field details.
- Unexpected errors return HTTP 500 with a correlation id.
- Use the embedded metrics namespace to chart p95 query timings and rows returned.
- Keep CloudWatch log retention to a sensible period such as thirty days.

## 13. Appendix templates

Keep `openapi.yaml` at the repository root and update it whenever the Pydantic model changes. You can generate static documentation from it with Redoc or Swagger UI in a separate step.

```yaml
openapi: 3.0.3
info:
  title: ACLED Trendfinder API
  version: 1.0.0
paths:
  /trendfinder:
    get:
      summary: Query events with filters and pagination
      parameters:
        - in: query
          name: country
          required: true
          schema: { type: string, minLength: 2 }
        - in: query
          name: start_date
          required: true
          schema: { type: string, format: date }
        - in: query
          name: end_date
          required: true
          schema: { type: string, format: date }
        - in: query
          name: page
          schema: { type: integer, minimum: 1, default: 1 }
        - in: query
          name: page_size
          schema: { type: integer, minimum: 1, maximum: 500, default: 50 }
        - in: query
          name: sort_by
          schema:
            type: string
            enum: [event_date, fatalities, country]
            default: event_date
        - in: query
          name: sort_dir
          schema:
            type: string
            enum: [asc, desc]
            default: desc
      responses:
        "200":
          description: Page of events
        "400":
          description: Validation error
```
