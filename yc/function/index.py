"""Cloud Function `afisha-api`: точка входа HTTP.

Сайт шлёт POST с Content-Type text/plain (простой запрос, без preflight),
в теле JSON: {"op":"load","code":…[, "since":…, "afisha_since":…]}
или {"op":"save","code":…, "data":{…}, "since":…}.
"""
import base64
import json
import os

import logic

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
}

_store = None


def store():
    global _store
    if _store is None:
        import ydb.iam
        from store import YdbStore
        _store = YdbStore(os.environ["YDB_ENDPOINT"], os.environ["YDB_DATABASE"],
                          ydb.iam.MetadataUrlCredentials())
    return _store


def reply(status, obj):
    h = dict(CORS)
    h["Content-Type"] = "application/json; charset=utf-8"
    h["Cache-Control"] = "no-store"
    return {"statusCode": status, "headers": h,
            "body": json.dumps(obj, ensure_ascii=False, separators=(",", ":"))}


def handler(event, context):
    method = (event or {}).get("httpMethod", "POST")
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": dict(CORS), "body": ""}
    if method != "POST":
        return reply(405, {"ok": False, "error": "bad_request"})
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return reply(400, {"ok": False, "error": "bad_request"})
    try:
        status, res = logic.handle(store(), body)
    except Exception as e:  # база недоступна и т. п.; сайт покажет «база временно недоступна»
        print("error:", type(e).__name__, e)
        return reply(503, {"ok": False, "error": "server"})
    return reply(status, res)
