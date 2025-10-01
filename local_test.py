import json
import os
import handler as h

# Read the first debug key if present in env (populated from config.ini by handler)
DEBUG_KEYS = os.getenv("DEBUG_KEYS", "")
DEBUG_KEY = DEBUG_KEYS.split(",")[0].strip() if DEBUG_KEYS else None

# ---- offline DB stub so you can run without a real database ----
class DummyCursor:
    def execute(self, sql, params=None):
        self._last_sql = sql
        self._last_params = params or []
    def fetchone(self):
        return {"total": 1}
    def fetchall(self):
        return [{
            "event_id": "TEST001",
            "event_date": "2025-01-01",
            "country": "Kenya",
            "admin1": "Nairobi",
            "event_type": "Protests",
            "sub_event_type": "Peaceful protest",
            "actor1": "Civic group",
            "actor2": "Police",
            "fatalities": 0,
            "latitude": -1.2921,
            "longitude": 36.8219
        }]

class DummyConn:
    @property
    def open(self):
        return True
    def cursor(self):
        return self
    def __enter__(self):
        return DummyCursor()
    def __exit__(self, exc_type, exc, tb):
        pass

# Monkey patch handler to bypass real DB
h.get_db_connection = lambda: DummyConn()

# Simulated API Gateway request with optional debug and correlation id
event = {
    "resource": "/trendfinder",
    "path": "/trendfinder",
    "httpMethod": "GET",
    "headers": {
        **({"X-Debug-Key": DEBUG_KEY} if DEBUG_KEY else {}),
        "X-Correlation-Id": "local-corr-0001"
    },
    "queryStringParameters": {
        "country": "Kenya",
        "start_date": "2025-01-01",
        "end_date": "2025-09-01",
        "sort_by": "event_date",
        "sort_dir": "desc",
        "page": "1",
        "page_size": "5"
    }
}

resp = h.handler(event, None)
print(resp["statusCode"])
print(json.dumps(json.loads(resp["body"]), indent=2))
