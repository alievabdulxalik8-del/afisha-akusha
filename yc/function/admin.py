"""Cloud Function `afisha-admin`: служебные операции с базой.

Закрытая (без allUsers): вызвать может только тот, у кого есть роль на каталог.
Нужна потому, что YDB говорит по gRPC, а до него не всегда можно достучаться
напрямую; по HTTPS до функции — можно. Коды сюда приходят уже хешем.
"""
import base64
import json
import os

import index
from store import Q_PUT, dump


def _schema(st):
    import ydb
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.yql")
    done = []
    for stmt in open(path, encoding="utf-8").read().split(";"):
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if not body:
            continue
        name = body.split()[2]
        try:
            st.pool.execute_with_retries(body + ";")
            done.append(name + ": создана")
        except ydb.Error as e:
            if "exist" in str(e).lower():
                done.append(name + ": уже есть")
            else:
                raise
    return done


def run(req):
    import ydb
    st = index.store()
    u = ydb.PrimitiveType.Utf8
    ou = ydb.OptionalType(u)
    s = ydb.PrimitiveType.String
    op = req.get("op")
    if op == "schema":
        return {"ok": True, "tables": _schema(st)}
    if op == "put_access":
        st.pool.execute_with_retries(
            "DECLARE $id AS Utf8; DECLARE $salt AS String; DECLARE $hash AS String; DECLARE $role AS Utf8; DECLARE $label AS Utf8;"
            "UPSERT INTO access (id, salt, hash, role, label) VALUES ($id, $salt, $hash, $role, $label);",
            {"$id": (req["id"], u), "$salt": (base64.b64decode(req["salt"]), s),
             "$hash": (base64.b64decode(req["hash"]), s), "$role": (req["role"], u), "$label": (req["label"], u)})
        return {"ok": True}
    if op == "put_state":
        q = Q_PUT.replace("DECLARE $by AS Utf8;", "DECLARE $by AS Utf8?;")
        st.pool.execute_with_retries(q, {"$id": (req["id"], u), "$data": (dump(req["data"]), u),
                                         "$at": (req["updated_at"], u), "$by": (req.get("updated_by"), ou)})
        return {"ok": True}
    if op == "get_state":
        return {"ok": True, "state": st.state(req["id"]), "meta": st.meta(),
                "access": [{"role": a["role"], "label": a["label"]} for a in st.access_rows()]}
    return {"ok": False, "error": "bad_request"}


def handler(event, context):
    body = (event or {}).get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        res = run(json.loads(body))
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False)}
    return {"statusCode": 200, "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(res, ensure_ascii=False)}
