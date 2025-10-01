import json
import os
import sys
import logging
import traceback
from datetime import datetime
from decimal import Decimal
from urllib.parse import parse_qs
import configparser
import pymysql

# ---------------------------------------------
# Optional local config loader
# Only loads config.ini when not running in Lambda
# Environment variables always take precedence
# ---------------------------------------------
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

# ---------------------------------------------
# Configuration
# ---------------------------------------------
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
DB_SSL_CA_PATH = os.getenv("DB_SSL_CA_PATH")

TABLE_NAME = os.getenv("TABLE_NAME", "events")
DATE_FIELD = os.getenv("DATE_FIELD", "event_date")

FILTERABLE_FIELDS = {
    "country": "country",
    "event_type": "event_type",
    "sub_event_type": "sub_event_type",
    "actor1": "actor1",
    "actor2": "actor2",
}

SORTABLE_FIELDS = {
    "event_date": "event_date",
    "fatalities": "fatalities",
    "country": "country",
}

DEFAULT_PAGE_SIZE = int(os.getenv("DEFAULT_PAGE_SIZE", "50"))
MAX_PAGE_SIZE = int(os.getenv("MAX_PAGE_SIZE", "500"))

# Debug and logging
DEBUG_ALLOWED_KEYS = set(k.strip() for k in (os.getenv("DEBUG_KEYS", "")).split(",") if k.strip())
DEBUG_MAX_ROWS = int(os.getenv("DEBUG_MAX_ROWS", "50"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
VERBOSE = os.getenv("VERBOSE", "false").lower() == "true"

# Connection cache for warm Lambda invocations
_connection = None

# Logger
logger = logging.getLogger()
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# ---------------------------------------------
# Helpers
# ---------------------------------------------
class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)

def log_json(level, **kwargs):
    try:
        msg = json.dumps(kwargs, cls=EnhancedJSONEncoder)
    except Exception:
        msg = str(kwargs)
    logger.log(level, msg)

def get_db_connection():
    global _connection
    if _connection and getattr(_connection, "open", False):
        return _connection

    ssl_args = None
    if DB_SSL_CA_PATH:
        ssl_args = {"ca": DB_SSL_CA_PATH}

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

def first(params, key):
    val = params.get(key)
    if isinstance(val, list):
        return val[0]
    return val

def to_int(s, default):
    try:
        return int(s)
    except Exception:
        return default

def is_http_api_v2(event):
    return "rawQueryString" in event

def parse_params(event):
    if is_http_api_v2(event):
        return parse_qs(event.get("rawQueryString", "") or "")
    qs = event.get("queryStringParameters") or {}
    return {k: [v] for k, v in qs.items()}

def build_filters(params, sql_params):
    where = []

    # Date range
    start_date = first(params, "start_date")
    end_date = first(params, "end_date")
    if start_date:
        where.append(f"{DATE_FIELD} >= %s")
        sql_params.append(start_date)
    if end_date:
        where.append(f"{DATE_FIELD} < %s")
        sql_params.append(end_date)

    # Allow listed equality filters
    for key, column in FILTERABLE_FIELDS.items():
        val = first(params, key)
        if val:
            where.append(f"{column} = %s")
            sql_params.append(val)

    # Example text search across two columns if they exist
    q = first(params, "q")
    if q:
        where.append("(title LIKE %s OR notes LIKE %s)")
        like = f"%{q}%"
        sql_params.extend([like, like])

    return " WHERE " + " AND ".join(where) if where else ""

def build_sort(params):
    sort_by = first(params, "sort_by") or DATE_FIELD
    sort_dir = first(params, "sort_dir") or "desc"
    column = SORTABLE_FIELDS.get(sort_by, DATE_FIELD)
    direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
    return f" ORDER BY {column} {direction}"

def get_pagination(params):
    page = to_int(first(params, "page"), 1)
    page = max(page, 1)
    page_size = to_int(first(params, "page_size"), DEFAULT_PAGE_SIZE)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    offset = (page - 1) * page_size
    return page, page_size, offset

def response(status_code, body_dict, cors=True, correlation_id=None):
    headers = {"Content-Type": "application/json"}
    if cors:
        headers.update({
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Debug-Key,X-Correlation-Id",
        })
    if correlation_id:
        headers["X-Correlation-Id"] = str(correlation_id)
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body_dict, cls=EnhancedJSONEncoder),
    }

def is_debug(event, params):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied = headers.get("x-debug-key") or first(params, "x-debug-key")
    if supplied and supplied in DEBUG_ALLOWED_KEYS:
        return True
    return False

# ---------------------------------------------
# Lambda handler
# ---------------------------------------------
def handler(event, context):
    corr_id = None
    try:
        params = parse_params(event)

        # Correlation ID for log search and tracing
        headers_lower = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        corr_id = headers_lower.get("x-correlation-id") or event.get("requestContext", {}).get("requestId")

        debug_on = is_debug(event, params)
        verbose_on = VERBOSE or debug_on

        log_json(logging.INFO, msg="request_received",
                 correlation_id=corr_id,
                 path=event.get("path"),
                 params={k: v if not isinstance(v, list) else v[:1] for k, v in (params or {}).items()},
                 debug_on=debug_on)

        page, page_size, offset = get_pagination(params)
        sql_params = []

        where_sql = build_filters(params, sql_params)
        order_sql = build_sort(params)

        if verbose_on:
            log_json(logging.DEBUG, msg="sql_planned",
                     correlation_id=corr_id,
                     where_sql=where_sql,
                     order_sql=order_sql,
                     params_preview=[str(p)[:64] for p in sql_params],
                     page=page, page_size=page_size, offset=offset)

        # Select a curated set of columns
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

        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(count_sql, sql_params)
            total = cur.fetchone()["total"]
            cur.execute(select_sql, sql_params + [page_size, offset])
            rows = cur.fetchall()

        if debug_on:
            rows = rows[:DEBUG_MAX_ROWS]

        total_pages = (total + page_size - 1) // page_size

        body = {
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "sort": {
                    "by": first(params, "sort_by") or DATE_FIELD,
                    "dir": first(params, "sort_dir") or "desc",
                },
                "filters": {k: v[0] for k, v in params.items()},
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
                    }
                    if debug_on
                    else None
                ),
            },
            "data": rows,
        }

        if verbose_on:
            log_json(logging.DEBUG, msg="sql_executed",
                     correlation_id=corr_id,
                     rows=len(rows), total=total)

        return response(200, body, correlation_id=corr_id)

    except Exception as ex:
        log_json(logging.ERROR, msg="unhandled_exception",
                 correlation_id=corr_id, error=str(ex))
        return response(
            500,
            {
                "error": "internal_error",
                "message": "An unexpected error occurred",
                "correlation_id": corr_id,
            },
            correlation_id=corr_id,
        )

# ---------------------------------------------
# Local runner for a quick smoke test
# Run:  python handler.py
# ---------------------------------------------
if __name__ == "__main__":
    event = {
        "path": "/trendfinder",
        "rawQueryString": "country=Kenya&page=1&page_size=5",
        "headers": {
            "X-Debug-Key": next(iter(DEBUG_ALLOWED_KEYS), ""),
            "X-Correlation-Id": "local-test-12345",
        },
    }
    print(handler(event, None))
