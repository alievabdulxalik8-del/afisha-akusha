#!/usr/bin/env python3
"""Развёртывание бэкенда афиши в Yandex Cloud (этапы 1–2 из MIGRATION.md).

Ключ сервисного аккаунта afisha-deploy берётся из переменных среды
YC_KEY_ID, YC_SA_ID, YC_PRIVATE_KEY. Ничего секретного скрипт не печатает.

    pip install pyjwt cryptography requests "ydb>=3.18,<4"
    python3 yc/deploy.py check        # ключ работает, каталог виден
    python3 yc/deploy.py setup        # YDB, таблицы, СА afisha-fn, функция (повторный запуск безопасен)
    python3 yc/deploy.py deploy       # только новая версия функции
    AFISHA_CODE_EDITOR=… AFISHA_CODE_VIEWER=… python3 yc/deploy.py copy     # копия данных из Supabase
    AFISHA_CODE_EDITOR=… AFISHA_CODE_VIEWER=… python3 yc/deploy.py verify   # сверка с Supabase и проверки
    python3 yc/deploy.py set-events events.json   # заменить афишу (строка events)

Коды доступа в YDB попадают только хешем; в выводе их нет.
"""
import base64
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "function"))
import logic  # noqa: E402

FOLDER_ID = "b1gi8forub14au4jm25l"
DB_NAME = "afisha-db"
FN_SA_NAME = "afisha-fn"
FN_NAME = "afisha-api"
SB_URL = "https://hyjyfmgskcjijyyblvtg.supabase.co"
SB_KEY = "sb_publishable_IRpG1zauJd1OLm36YCB40w_gZr4emDL"  # публичный, тот же, что в index.html
SITE_ORIGIN = "https://alievabdulxalik8-del.github.io"

IAM = "https://iam.api.cloud.yandex.net/iam/v1"
RM = "https://resource-manager.api.cloud.yandex.net/resource-manager/v1"
YDB_API = "https://ydb.api.cloud.yandex.net/ydb/v1"
FN_API = "https://serverless-functions.api.cloud.yandex.net/functions/v1"
OPS = "https://operation.api.cloud.yandex.net/operations"


# ---------- доступ к облаку ----------

def private_key():
    """YC_PRIVATE_KEY: переносы бывают настоящими, экранами \\n или пробелами;
    строку-предупреждение Яндекса перед BEGIN отбрасываем. Собираем PEM заново."""
    k = os.environ["YC_PRIVATE_KEY"].replace("\\r", " ").replace("\\n", " ")
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", k, re.S)
    if not m:
        sys.exit("YC_PRIVATE_KEY: не найден блок -----BEGIN … -----END")
    body = "".join(m.group(2).split())
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (m.group(1), "\n".join(lines), m.group(1))


_token = {}


def iam_token():
    if _token.get("exp", 0) > time.time() + 60:
        return _token["t"]
    import jwt
    now = int(time.time())
    enc = jwt.encode({"aud": IAM + "/tokens", "iss": os.environ["YC_SA_ID"].strip(), "iat": now, "exp": now + 3600},
                     private_key(), algorithm="PS256", headers={"kid": os.environ["YC_KEY_ID"].strip()})
    r = requests.post(IAM + "/tokens", json={"jwt": enc}, timeout=30)
    if r.status_code != 200:
        sys.exit("IAM не выдал токен: %s %s" % (r.status_code, r.text[:300]))
    _token.update(t=r.json()["iamToken"], exp=now + 3000)
    return _token["t"]


def call(method, url, body=None, ok=(200,)):
    r = requests.request(method, url, json=body, timeout=60, headers={"Authorization": "Bearer " + iam_token()})
    if r.status_code not in ok:
        raise RuntimeError("%s %s → %s %s" % (method, url, r.status_code, r.text[:500]))
    return r.json() if r.text else {}


def wait_op(op):
    while not op.get("done"):
        time.sleep(3)
        op = call("GET", OPS + "/" + op["id"])
    if "error" in op:
        raise RuntimeError("операция %s: %s" % (op.get("description"), op["error"]))
    return op.get("response", {})


def find(url, key, name):
    for x in call("GET", url + "?folderId=" + FOLDER_ID).get(key, []):
        if x.get("name") == name:
            return x
    return None


# ---------- ресурсы ----------

def ensure_db():
    db = find(YDB_API + "/databases", "databases", DB_NAME)
    if not db:
        print("создаю YDB", DB_NAME)
        db = wait_op(call("POST", YDB_API + "/databases",
                          {"folderId": FOLDER_ID, "name": DB_NAME, "serverlessDatabase": {}}))
    while db.get("status") not in ("RUNNING",):
        print("  YDB:", db.get("status"))
        time.sleep(5)
        db = call("GET", YDB_API + "/databases/" + db["id"])
    return db


def db_conn(db):
    ep = db["endpoint"]  # grpcs://ydb.serverless.yandexcloud.net:2135/?database=/ru-central1/…/…
    host, _, q = ep.partition("/?database=")
    return host, q


def ydb_pool(db):
    import ydb
    host, path = db_conn(db)
    driver = ydb.Driver(endpoint=host, database=path, credentials=ydb.AccessTokenCredentials(iam_token()))
    driver.wait(timeout=20, fail_fast=True)
    return ydb.QuerySessionPool(driver)


def ensure_tables(db):
    pool = ydb_pool(db)
    rows = pool.execute_with_retries("SELECT 1 AS x;")  # связь есть
    assert rows
    for stmt in open(os.path.join(HERE, "schema.yql"), encoding="utf-8").read().split(";"):
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if not body:
            continue
        name = body.split()[2]
        try:
            pool.execute_with_retries(body + ";")
            print("  таблица", name, "создана")
        except Exception as e:
            if "already exists" in str(e) or "path exist" in str(e).lower():
                print("  таблица", name, "уже есть")
            else:
                raise


def ensure_fn_sa():
    sa = find(IAM + "/serviceAccounts", "serviceAccounts", FN_SA_NAME)
    if not sa:
        print("создаю сервисный аккаунт", FN_SA_NAME)
        sa = wait_op(call("POST", IAM + "/serviceAccounts",
                          {"folderId": FOLDER_ID, "name": FN_SA_NAME, "description": "функция афиши: только ydb.editor"}))
    try:
        wait_op(call("POST", RM + "/folders/%s:updateAccessBindings" % FOLDER_ID, {"accessBindingDeltas": [
            {"action": "ADD", "accessBinding": {"roleId": "ydb.editor",
                                                "subject": {"id": sa["id"], "type": "serviceAccount"}}}]}))
        print("  роль ydb.editor у", FN_SA_NAME, "есть")
    except RuntimeError as e:
        if "already" in str(e).lower():
            print("  роль ydb.editor у", FN_SA_NAME, "уже была")
        elif "403" in str(e) or "PERMISSION" in str(e).upper():
            print("\n!! Роль editor не позволяет выдавать роли. Выдайте вручную: консоль → каталог afisha →\n"
                  "   Права доступа → Назначить роли → сервисный аккаунт afisha-fn → ydb.editor.\n")
        else:
            raise
    return sa


def fn_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("index.py", "logic.py", "store.py", "requirements.txt"):
            z.write(os.path.join(HERE, "function", f), f)
    return base64.b64encode(buf.getvalue()).decode()


def ensure_fn():
    fn = find(FN_API + "/functions", "functions", FN_NAME)
    if not fn:
        print("создаю функцию", FN_NAME)
        fn = wait_op(call("POST", FN_API + "/functions",
                          {"folderId": FOLDER_ID, "name": FN_NAME, "description": "бэкенд афиши"}))
    try:
        wait_op(call("POST", FN_API + "/functions/%s:updateAccessBindings" % fn["id"], {"accessBindingDeltas": [
            {"action": "ADD", "accessBinding": {"roleId": "functions.functionInvoker",
                                                "subject": {"id": "allUsers", "type": "system"}}}]}))
    except RuntimeError as e:
        if "already" not in str(e).lower():
            raise
    return fn


def deploy_version(fn, sa, db):
    host, path = db_conn(db)
    print("выкладываю версию функции…")
    wait_op(call("POST", FN_API + "/versions", {
        "functionId": fn["id"],
        "runtime": "python312",
        "entrypoint": "index.handler",
        "resources": {"memory": str(128 * 1024 * 1024)},
        "executionTimeout": "10s",
        "serviceAccountId": sa["id"],
        "content": fn_zip(),
        "environment": {"YDB_ENDPOINT": host, "YDB_DATABASE": path},
    }))
    print("готово:", fn_url(fn))


def fn_url(fn):
    return fn.get("httpInvokeUrl") or "https://functions.yandexcloud.net/" + fn["id"]


# ---------- данные ----------

def sb_load(code):
    r = requests.post(SB_URL + "/rest/v1/rpc/afisha_load", json={"p_code": code}, timeout=30,
                      headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY})
    r.raise_for_status()
    return r.json()


def codes():
    out = []
    for env in ("AFISHA_CODE_EDITOR", "AFISHA_CODE_VIEWER"):
        c = os.environ.get(env, "").strip().lower()  # сайт приводит код к нижнему регистру
        if not c:
            sys.exit("нужна переменная " + env)
        out.append(c)
    return out


def copy_data(db):
    import ydb
    pool = ydb_pool(db)
    ed, vw = codes()
    a, v = sb_load(ed), sb_load(vw)
    if not (a.get("ok") and a.get("role") == "editor" and v.get("ok") and v.get("role") == "viewer"):
        sys.exit("Supabase не принял коды или роли не те (editor/viewer)")
    u = ydb.PrimitiveType.Utf8
    s = ydb.PrimitiveType.String
    for rid, code, j in (("editor", ed, a), ("viewer", vw, v)):
        salt = os.urandom(16)
        pool.execute_with_retries(
            "DECLARE $id AS Utf8; DECLARE $salt AS String; DECLARE $hash AS String; DECLARE $role AS Utf8; DECLARE $label AS Utf8;"
            "UPSERT INTO access (id, salt, hash, role, label) VALUES ($id, $salt, $hash, $role, $label);",
            {"$id": (rid, u), "$salt": (salt, s), "$hash": (logic.hash_code(code, salt), s),
             "$role": (j["role"], u), "$label": (j["label"], u)})
    for sid, data, at, by in (("akusha", a["data"], a["updated_at"], a["updated_by"]),
                              ("events", a["afisha"], a["afisha_updated_at"], None)):
        put_state(pool, sid, data, at, by)
    print("скопировано: коды (хешем), akusha %d Б, events %d Б" % (
        len(json.dumps(a["data"], ensure_ascii=False).encode()), len(json.dumps(a["afisha"], ensure_ascii=False).encode())))


def put_state(pool, sid, data, at, by):
    import ydb
    from store import Q_PUT, dump
    u = ydb.PrimitiveType.Utf8
    q = Q_PUT.replace("DECLARE $by AS Utf8;", "DECLARE $by AS Utf8?;")
    pool.execute_with_retries(q, {"$id": (sid, u), "$data": (dump(data), u), "$at": (at, u),
                                  "$by": (by, ydb.OptionalType(u))})


def set_events(db, path):
    af = json.load(open(path, encoding="utf-8"))
    if not isinstance(af.get("events"), list) or not af["events"]:
        sys.exit("в файле нет списка events")
    put_state(ydb_pool(db), "events", af, logic.now_iso(), None)
    print("афиша заменена: %d мероприятий, версия сайта %s" % (len(af["events"]), (af.get("site") or {}).get("version")))


# ---------- проверки (этап 2) ----------

def canon(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def verify(fn):
    url = fn_url(fn)
    ed, vw = codes()
    fails = []

    def yc(q, origin=SITE_ORIGIN):
        r = requests.post(url, data=json.dumps(q, ensure_ascii=False).encode(), timeout=30,
                          headers={"Content-Type": "text/plain;charset=UTF-8", "Origin": origin})
        return r, (r.json() if r.headers.get("Content-Type", "").startswith("application/json") else None)

    def check(name, cond):
        print(("  ок  " if cond else "  ОШИБКА ") + name)
        if not cond:
            fails.append(name)

    for code, role in ((ed, "editor"), (vw, "viewer")):
        s = sb_load(code)
        r, y = yc({"op": "load", "code": code})
        check("%s: вход, роль и подпись" % role, y and y.get("ok") and (y["role"], y["label"]) == (s["role"], s["label"]))
        check("%s: данные совпадают побайтно (канонический JSON)" % role, y and canon(y["data"]) == canon(s["data"]))
        check("%s: афиша совпадает" % role, y and canon(y["afisha"]) == canon(s["afisha"]))
        check("%s: отметки времени совпадают" % role, y and (y["updated_at"], y["afisha_updated_at"], y["updated_by"]) ==
              (s["updated_at"], s["afisha_updated_at"], s["updated_by"]))
    r, y = yc({"op": "load", "code": vw})
    r2, p = yc({"op": "load", "code": vw, "since": y["updated_at"], "afisha_since": y["afisha_updated_at"]})
    check("опрос: «без изменений» без данных", p and p.get("unchanged") is True and "data" not in p)
    check("неверный код", yc({"op": "load", "code": "zz-not-a-code"})[1] == {"ok": False, "error": "bad_code"})
    check("куратор не пишет", yc({"op": "save", "code": vw, "data": y["data"], "since": y["updated_at"]})[1] ==
          {"ok": False, "error": "read_only"})
    c = yc({"op": "save", "code": ed, "data": {"schools": []}, "since": "2000-01-01T00:00:00+00:00"})[1]
    check("конфликт при устаревшей отметке, данные не тронуты",
          c and c.get("error") == "conflict" and canon(c["data"]) == canon(y["data"]))
    check("CORS: ответ открыт сайту", r.headers.get("Access-Control-Allow-Origin") == "*")
    o = requests.options(url, headers={"Origin": SITE_ORIGIN, "Access-Control-Request-Method": "POST"}, timeout=30)
    check("CORS: OPTIONS отвечает", o.status_code in (200, 204) and o.headers.get("Access-Control-Allow-Origin") == "*")
    t = time.time()
    for _ in range(5):
        yc({"op": "load", "code": vw, "since": y["updated_at"], "afisha_since": y["afisha_updated_at"]})
    print("  опрос: %.0f мс на вызов (тёплая функция)" % ((time.time() - t) / 5 * 1000))
    print("\nRU за опрос: консоль → YDB afisha-db → Мониторинг → «Request units», до и после 100 опросов.")
    if fails:
        sys.exit("\nНе прошло: %d" % len(fails))
    print("\nВсе проверки прошли. Записи не было — данные в YDB не менялись.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        iam_token()
        call("GET", RM + "/folders/" + FOLDER_ID)
        print("ключ работает, каталог afisha виден")
        return
    if cmd in ("setup", "deploy"):
        db = ensure_db()
        if cmd == "setup":
            ensure_tables(db)
        deploy_version(ensure_fn(), ensure_fn_sa(), db)
        return
    db = find(YDB_API + "/databases", "databases", DB_NAME) or sys.exit("нет базы, сначала setup")
    if cmd == "copy":
        copy_data(db)
    elif cmd == "verify":
        verify(find(FN_API + "/functions", "functions", FN_NAME) or sys.exit("нет функции, сначала setup"))
    elif cmd == "set-events":
        set_events(db, sys.argv[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
