"""Логика бэкенда афиши, без привязки к базе.

Ответы повторяют Supabase RPC `afisha_load` / `afisha_save` один в один,
чтобы интерфейс сайта не менялся. Хранилище передаётся снаружи (см. store.py),
поэтому логику можно проверить без облака (tests/test_logic.py).
"""
import datetime
import hashlib
import hmac
import json
import time

STATE_ID = "akusha"
EVENTS_ID = "events"
KASSA_ID = "kassa"   # продажи из кабинета kassir.ru, пишет функция afisha-kassa
NOT_SENT = object()

# scrypt: ~16 МБ памяти и десятки миллисекунд на проверку — дорого для перебора
# украденных хешей. Живой экземпляр функции помнит уже проверенные коды,
# так что опрос раз в 25 с scrypt не повторяет. Память живёт 10 минут:
# сменённый в базе код перестаёт действовать не позже чем через 10 минут.
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
KNOWN_TTL = 600
_known = {}


def hash_code(code, salt):
    return hashlib.scrypt(code.encode("utf-8"), salt=salt, **SCRYPT)


def now_iso():
    # тот же вид, что у Postgres timestamptz в jsonb: 2026-09-25T10:11:12.123456+00:00
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def parse_ts(s):
    if not s:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t


def check_code(store, code):
    """(role, label) или None. Код сравнивается по хешу, без раннего выхода."""
    if not isinstance(code, str) or not code or len(code) > 200:
        return None
    key = hashlib.sha256(code.encode("utf-8")).hexdigest()
    hit = _known.get(key)
    if hit and time.monotonic() - hit[1] < KNOWN_TTL:
        return hit[0]
    found = None
    for row in store.access_rows():
        if hmac.compare_digest(hash_code(code, row["salt"]), row["hash"]):
            found = (row["role"], row["label"])
    if found:
        _known[key] = (found, time.monotonic())
    return found


def forget_codes():
    _known.clear()


def op_load(store, code, since=None, afisha_since=None, kassa_since=NOT_SENT):
    who = check_code(store, code)
    if not who:
        return {"ok": False, "error": "bad_code"}
    role, label = who
    if since is not None or afisha_since is not None:
        # дешёвый опрос: сначала только отметки времени
        meta = store.meta()
        same_kassa = kassa_since is NOT_SENT or meta.get(KASSA_ID) == kassa_since
        if meta.get(STATE_ID) == since and meta.get(EVENTS_ID) == afisha_since and same_kassa:
            return {"ok": True, "unchanged": True, "role": role, "label": label,
                    "updated_at": since, "afisha_updated_at": afisha_since}
    st = store.state(STATE_ID) or {}
    af = store.state(EVENTS_ID) or {}
    ka = store.state(KASSA_ID) or {}
    return {
        "ok": True,
        "role": role,
        "label": label,
        "data": st.get("data"),
        "updated_at": st.get("updated_at"),
        "updated_by": st.get("updated_by"),
        "afisha": af.get("data") if af.get("data") is not None else {},
        "afisha_updated_at": af.get("updated_at"),
        "kassa": ka.get("data"),
        "kassa_updated_at": ka.get("updated_at"),
    }


def op_save(store, code, data, since):
    who = check_code(store, code)
    if not who:
        return {"ok": False, "error": "bad_code"}
    role, label = who
    if role != "editor":
        return {"ok": False, "error": "read_only"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "bad_request"}
    p_since = parse_ts(since)

    def attempt(tx):
        cur = tx.state(STATE_ID) or {}
        cur_at = parse_ts(cur.get("updated_at"))
        if p_since is not None and cur_at is not None and cur_at > p_since:
            return {"ok": False, "error": "conflict", "updated_at": cur.get("updated_at"),
                    "updated_by": cur.get("updated_by"), "data": cur.get("data")}
        at = now_iso()
        tx.put_state(STATE_ID, data, at, label)
        return {"ok": True, "updated_at": at, "updated_by": label}

    return store.in_tx(attempt)


# кнопка «Обновить из кассы»: чаще раза в 3 минуты кассу не дёргаем
KASSA_GAP = 180


def op_kassa_refresh(store, code, refresh):
    """Сходить в кассу сейчас (refresh() вызывает функцию afisha-kassa) и вернуть свежие данные."""
    if not check_code(store, code):
        return {"ok": False, "error": "bad_code"}
    last = parse_ts(store.meta().get(KASSA_ID))
    fresh = last is not None and (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() < KASSA_GAP
    if not fresh:
        if refresh is None:
            return {"ok": False, "error": "kassa", "detail": "обновление из кассы не настроено"}
        err = refresh()
        if err:
            return {"ok": False, "error": "kassa", "detail": err}
    res = op_load(store, code)
    res["refreshed"] = not fresh
    return res


MAX_BODY = 2 * 1024 * 1024


def handle(store, body_text, refresh=None):
    """Разбор запроса сайта: {"op":"load"|"save"|"kassa_refresh", "code":…, …}."""
    if not body_text or len(body_text) > MAX_BODY:
        return 400, {"ok": False, "error": "bad_request"}
    try:
        req = json.loads(body_text)
    except ValueError:
        return 400, {"ok": False, "error": "bad_request"}
    if not isinstance(req, dict):
        return 400, {"ok": False, "error": "bad_request"}
    op = req.get("op")
    if op == "load":
        return 200, op_load(store, req.get("code"), req.get("since"), req.get("afisha_since"),
                            req["kassa_since"] if "kassa_since" in req else NOT_SENT)
    if op == "kassa_refresh":
        return 200, op_kassa_refresh(store, req.get("code"), refresh)
    if op == "save":
        res = op_save(store, req.get("code"), req.get("data"), req.get("since"))
        return (400 if res.get("error") == "bad_request" else 200), res
    return 400, {"ok": False, "error": "bad_request"}
