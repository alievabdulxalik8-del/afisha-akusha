"""Cloud Function `afisha-api`: точка входа HTTP.

Сайт шлёт POST с Content-Type text/plain (простой запрос, без preflight),
в теле JSON: {"op":"load","code":…[, "since":…, "afisha_since":…]},
{"op":"save","code":…, "data":{…}, "since":…} или {"op":"kassa_refresh","code":…}.
"""
import base64
import json
import os
import urllib.request

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


def kassa_caller(context):
    """Вызов закрытой функции afisha-kassa от имени сервисного аккаунта этой функции."""
    url = os.environ.get("KASSA_URL")
    token = (getattr(context, "token", None) or {}).get("access_token")
    if not url or not token:
        return None

    def refresh():
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=110) as r:
                res = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print("kassa:", type(e).__name__, e)
            return "касса не ответила"
        return None if res.get("ok") else "; ".join(res.get("errors") or [res.get("error") or "ошибка кассы"])
    return refresh


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
        status, res = logic.handle(store(), body, kassa_caller(context))
    except Exception as e:  # база недоступна и т. п.; сайт покажет «база временно недоступна»
        print("error:", type(e).__name__, e)
        return reply(503, {"ok": False, "error": "server"})
    return reply(status, res)
