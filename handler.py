"""
ACLED Trendfinder API handler with Embedded Metrics

What this adds
--------------
1) Request validation with Pydantic that mirrors openapi.yaml
2) CloudWatch Embedded Metrics (EMF) for:
     - total handler time
     - DB connect time
     - COUNT query time
     - SELECT query time
     - rows returned and total rows
     - page size
     - cold start
   You can graph these as p50 p90 p95 p99 in CloudWatch.

Environment to consider
-----------------------
METRICS_NAMESPACE   default: ACLED/Trendfinder
METRICS_STAGE       default: dev
METRICS_ENABLED     default: true
DEBUG_KEYS          comma separated, enables richer response meta
LOG_LEVEL           INFO by default
VERBOSE             false by default
"""

import json
import os
import logging
from time import perf_counter
from datetime import datetime, date
from decimal import Decimal
from urllib.parse import parse_qs
import configparser
import pymysql

from typing import Literal, Optional
from pydantic import BaseModel, ValidationError, constr, conint

# Embedded metrics
from aws_embedded_metrics import metric_scope

# -------------------------------------------------------------------
# Local config loader for laptop runs
# -------------------------------------------------------------------
def _load_local_config():
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return
    cfg_path = os.getenv("CONFIG_PATH", "config.ini")
    if os.path.exists(cfg_path):
        parser = configparser.ConfigParser()
        parser.read(cfg_path)
        for k, v in parser.defaults().items():
            if k.upper() not in os.environ and v is not None:
                os.environ[k.upper()] = v

_load_local_config()

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
DB_SSL_CA_PATH = os.getenv("DB_SSL_CA_PATH")

TABLE_NAME = os.getenv("TABLE_NAME", "events")
DATE_FIELD = os.getenv("DATE_FIELD", "event_date")

DEFAULT_PAGE_SIZE = int(os.getenv("DEFAULT_PAGE_SIZE", "50"))
MAX_PAGE_SIZE = int(os.getenv("MAX_PAGE_SIZE", "500"))

FILTERABLE_FIELDS = {
    "country": "country",
    "event_type": "event_type",
    "sub_event_type": "sub_event_type",
    "actor1": "actor1",
    "actor2": "actor2",
}
SORTABLE_FIELDS = {"event_date": "event_date", "fatalities": "fatalities", "country": "country"}

DEBUG_ALLOWED_KEYS = {k.strip() for k in os.getenv("DEBUG_KEYS", "").split(",") if k.strip()}
DEBUG_MAX_ROWS = int(os.getenv("DEBUG_MAX_ROWS", "50"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
VERBOSE = os.getenv("VERBOSE", "false").lower() == "true"

METRICS_NAMESPACE = os.getenv("METRICS_NAMESPACE", "ACLED/Trendfinder")
METRICS_STAGE = os.getenv("METRICS_STAGE", "dev")
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").lower() == "true"

# Warm start detection
_COLD_START = True

# Connection cache
_connection = None

# Logger
logger = logging.getLogger()
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# -------------------------------------------------------------------
# Helpers: JSON and logs
# -------------------------------------------------------------------
class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)

def log_json(level, **kwargs):
    try:
        msg = json.dumps(kwargs, cls=EnhancedJSONEncoder)
    except Exception:
        msg = str(kwargs)
    logger.log(level, msg)

# -------------------------------------------------------------------
# OpenAPI style validation with Pydantic
# -------------------------------------------------------------------
class TrendfinderQuery(BaseModel):
    country: constr(strip_whitespace=True, min_length=2)
    start_date: date
    end_date: date

    page: conint(ge=1) = 1
    page_size: conint(ge=1, le=MAX_PAGE_SIZE) = DEFAULT_PAGE_SIZE

    sort_by: Literal["event_date", "fatalities", "country"] = "event_date"
    sort_dir: Literal["asc", "desc"] = "desc"

    q: Optional[constr(strip_whitespace=True, min_length=1, max_length=200)] = None
    event_type: Optional[constr(strip_whitespace=True, min_length=1)] = None
    sub_event_type: Optional[constr(strip_whitespace=True, min_length=1)] = None
    actor1: Optional[constr(strip_whitespace=True, min_length=1)] = None
    actor2: Optional[constr(strip_whitespace=True, min_length=1)] = None

# -------------------------------------------------------------------
# HTTP helpers
# -------------------------------------------------------------------
def is_http_api_v2(event) -> bool:
    return "rawQueryString" in event

def parse_params(event) -> dict:
    if is_http_api_v2(event):
        return parse_qs(event.get("rawQueryString", "") or "")
    qs = event.get("queryStringParameters") or {}
    return {k: [v] for k, v in qs.items()}

def collapse_to_simple(params: dict) -> dict:
    return {k: (v[0] if isinstance(v, list) and v else v) for k, v in params.items()}

def response(status_code: int, body_dict: dict, cors: bool = True, correlation_id: str = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if cors:
        headers.update({
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Debug-Key,X-Correlation-Id",
        })
    if correlation_id:
        headers["X-Correlation-Id"] = str(correlation_id)
    return {"statusCode": status_code, "headers": headers, "body": json.dumps(body_dict, cls=EnhancedJSONEncoder)}

def is_debug(event, params) -> bool:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied = headers.get("x-debug-key") or collapse_to_simple(params).get("x-debug-key")
    return bool(supplied and supplied in DEBUG_ALLOWED_KEYS)

# -------------------------------------------------------------------
# SQL helpers
# -------------------------------------------------------------------
def build_filters_from_validated(q: TrendfinderQuery, sql_params: list) -> str:
    where = []
    if q.start_date:
        where.append(f"{DATE_FIELD} >= %s")
        sql_params.append(q.start_date.isoformat())
    if q.end_date:
        where.append(f"{DATE_FIELD} < %s")
        sql_params.append(q.end_date.isoformat())
    for key, column in FILTERABLE_FIELDS.items():
        val = getattr(q, key, None)
        if val:
            where.append(f"{column} = %s")
            sql_params.append(val)
    if q.q:
        where.append("(title LIKE %s OR notes LIKE %s)")
        like = f"%{q.q}%"
        sql_params.extend([like, like])
    return " WHERE " + " AND ".join(where) if where else ""

def build_sort_from_validated(q: TrendfinderQuery) -> str:
    column = SORTABLE_FIELDS.get(q.sort_by, DATE_FIELD)
    direction = "DESC" if q.sort_dir.lower() == "desc" else "ASC"
    return f" ORDER BY {column} {direction}"

def get_db_connection():
    global _connection
    if _connection and getattr(_connection, "open", False):
        return _connection
    ssl_args = {"ca": DB_SSL_CA_PATH} if DB_SSL_CA_PATH else None
    _connection = pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
        port=DB_PORT,
        ssl=ssl_args,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )
    return _connection

# -------------------------------------------------------------------
# Handler with embedded metrics
# -------------------------------------------------------------------
@metric_scope
def _handler(event, context, metrics):
    """
    The metric_scope decorator creates a metrics logger that writes EMF JSON to CloudWatch Logs.
    CloudWatch extracts metrics and you can graph p95 without code changes.
    """
    global _COLD_START

    # Namespace and base dimensions once per invocation
    metrics.set_namespace(METRICS_NAMESPACE)
    metrics.put_dimensions({
        "Service": "TrendfinderAPI",
        "Stage": METRICS_STAGE,
        "Function": os.getenv("AWS_LAMBDA_FUNCTION_NAME") or "local",
    })

    # Mark cold start once
    if _COLD_START:
        metrics.put_metric("ColdStart", 1, "Count")
        _COLD_START = False

    start_all = perf_counter()

    corr_id = None
    try:
        params = parse_params(event)
        headers_lower = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        corr_id = headers_lower.get("x-correlation-id") or event.get("requestContext", {}).get("requestId")
        debug_on = is_debug(event, params)
        verbose_on = VERBOSE or debug_on

        # add context as EMF properties for search later
        metrics.set_property("correlation_id", corr_id)
        metrics.set_property("path", event.get("path"))
        metrics.set_property("raw_query", event.get("rawQueryString"))

        log_json(logging.INFO, msg="request_received", correlation_id=corr_id,
                 path=event.get("path"), raw_query=event.get("rawQueryString"),
                 params_preview=collapse_to_simple(params), debug_on=debug_on)

        # Validation
        t0 = perf_counter()
        simple = collapse_to_simple(params)
        try:
            q = TrendfinderQuery(**simple)
        except ValidationError as ve:
            # record validation timing and failure
            metrics.put_metric("ValidationTimeMs", (perf_counter() - t0) * 1000.0, "Milliseconds")
            metrics.put_metric("RequestsRejected", 1, "Count")
            err_body = {
                "error": "bad_request",
                "message": "Request did not match the API contract",
                "details": ve.errors(),
                "correlation_id": corr_id,
            }
            log_json(logging.INFO, msg="request_rejected_validation", correlation_id=corr_id, errors=ve.errors())
            return response(400, err_body, correlation_id=corr_id)

        metrics.put_metric("ValidationTimeMs", (perf_counter() - t0) * 1000.0, "Milliseconds")
        metrics.set_property("country", q.country)
        metrics.set_property("sort_by", q.sort_by)
        metrics.set_property("sort_dir", q.sort_dir)
        metrics.put_metric("PageSize", int(q.page_size), "Count")
        metrics.set_property("debug_on", debug_on)

        # SQL plan
        sql_params = []
        where_sql = build_filters_from_validated(q, sql_params)
        order_sql = build_sort_from_validated(q)

        if verbose_on:
            log_json(logging.DEBUG, msg="sql_planned", correlation_id=corr_id,
                     where_sql=where_sql, order_sql=order_sql,
                     params_preview=[str(p)[:64] for p in sql_params],
                     page=q.page, page_size=q.page_size, offset=(q.page - 1) * q.page_size)

        # DB connect
        t_conn = perf_counter()
        conn = get_db_connection()
        metrics.put_metric("DBConnectMs", (perf_counter() - t_conn) * 1000.0, "Milliseconds")

        # Queries
        columns = [
            "event_id",
            f"{DATE_FIELD} AS event_date",
            "country",
            "admin1",
            "event_type",
            "sub_event_type",
            "actor1",
            "actor2",
            "fatalities",
            "latitude",
            "longitude",
        ]
        select_sql = f"SELECT {', '.join(columns)} FROM {TABLE_NAME}{where_sql}{order_sql} LIMIT %s OFFSET %s"
        count_sql = f"SELECT COUNT(1) AS total FROM {TABLE_NAME}{where_sql}"

        page = int(q.page)
        page_size = int(q.page_size)
        offset = (page - 1) * page_size

        with conn.cursor() as cur:
            t_count = perf_counter()
            cur.execute(count_sql, sql_params)
            total = cur.fetchone()["total"]
            metrics.put_metric("SQLCountMs", (perf_counter() - t_count) * 1000.0, "Milliseconds")

            t_select = perf_counter()
            cur.execute(select_sql, sql_params + [page_size, offset])
            rows = cur.fetchall()
            metrics.put_metric("SQLSelectMs", (perf_counter() - t_select) * 1000.0, "Milliseconds")

        if debug_on:
            rows = rows[:DEBUG_MAX_ROWS]

        total_pages = (total + page_size - 1) // page_size

        # Size and totals
        metrics.put_metric("RowsReturned", len(rows), "Count")
        metrics.put_metric("RowsTotal", int(total), "Count")

        body = {
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "sort": {"by": q.sort_by, "dir": q.sort_dir},
                "filters": {k: v for k, v in simple.items()},
                "correlation_id": corr_id,
                "debug": (
                    {
                        "where_sql": where_sql,
                        "order_sql": order_sql,
                        "params_preview": [str(p)[:64] for p in sql_params],
                        "limits": {"page_size": page_size, "hard_cap": MAX_PAGE_SIZE},
                        "lambda": {
                            "function": os.getenv("AWS_LAMBDA_FUNCTION_NAME"),
                            "memory_mb": os.getenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE"),
                            "log_stream": os.getenv("AWS_LAMBDA_LOG_STREAM_NAME"),
                        },
                        "validation": "ok",
                    } if debug_on else None
                ),
            },
            "data": rows,
        }

        if VERBOSE or debug_on:
            log_json(logging.DEBUG, msg="sql_executed", correlation_id=corr_id, rows=len(rows), total=total)

        # Overall time
        metrics.put_metric("HandlerDurationMs", (perf_counter() - start_all) * 1000.0, "Milliseconds")

        # Flush is automatic at return with metric_scope
        return response(200, body, correlation_id=corr_id)

    except Exception as ex:
        metrics.put_metric("Errors", 1, "Count")
        metrics.put_metric("HandlerDurationMs", (perf_counter() - start_all) * 1000.0, "Milliseconds")
        log_json(logging.ERROR, msg="unhandled_exception", correlation_id=corr_id, error=str(ex))
        return response(
            500,
            {"error": "internal_error", "message": "An unexpected error occurred", "correlation_id": corr_id},
            correlation_id=corr_id,
        )

def handler(event, context):
    """
    Public Lambda entry point.
    Separated so we can keep a standard (event, context) signature.
    """
    if not METRICS_ENABLED:
        # Fallback without EMF if disabled
        return _handler.__wrapped__(event, context)  # call the inner logic without metrics decorator
    return _handler(event, context)

# -------------------------------------------------------------------
# Local smoke test
# -------------------------------------------------------------------
if __name__ == "__main__":
    event = {
        "path": "/trendfinder",
        "rawQueryString": "country=Kenya&start_date=2025-01-01&end_date=2025-09-01&page=1&page_size=5",
        "headers": {"X-Correlation-Id": "local-test-12345"},
    }
    print(handler(event, None))
