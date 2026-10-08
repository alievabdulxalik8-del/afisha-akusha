"""Проверки логики бэкенда без облака: python3 -m unittest discover -s yc/tests"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "function"))
import logic  # noqa: E402


class MemStore:
    def __init__(self, codes):
        self.access = []
        for code, role, label in codes:
            salt = os.urandom(16)
            self.access.append({"salt": salt, "hash": logic.hash_code(code, salt), "role": role, "label": label})
        self.rows = {}
        self.scans = 0

    def access_rows(self):
        self.scans += 1
        return list(self.access)

    def meta(self):
        return {k: v["updated_at"] for k, v in self.rows.items()}

    def state(self, sid):
        r = self.rows.get(sid)
        return copy.deepcopy(r) if r else None

    def put_state(self, sid, data, at, by):
        self.rows[sid] = {"data": json.loads(json.dumps(data)), "updated_at": at, "updated_by": by}

    def in_tx(self, fn):
        return fn(self)


DATA = {"schools": [{"id": "s1", "name": "Акушинская СОШ №1"}], "seats": {}, "plan": {}, "history": []}
AFISHA = {"events": [{"id": "e1"}], "venues": [], "site": {"version": "2026-09-25-13"}}


def req(store, **kw):
    return logic.handle(store, json.dumps(kw, ensure_ascii=False))


class LogicTest(unittest.TestCase):
    def setUp(self):
        logic.forget_codes()
        self.s = MemStore([("org-code", "editor", "Организатор"), ("kur-code", "viewer", "Куратор")])
        self.s.put_state("akusha", DATA, "2026-09-25T10:00:00.000001+00:00", "Организатор")
        self.s.put_state("events", AFISHA, "2026-09-20T08:00:00.000000+00:00", None)

    def test_load_editor(self):
        st, r = req(self.s, op="load", code="org-code")
        self.assertEqual(st, 200)
        self.assertEqual(set(r), {"ok", "role", "label", "data", "updated_at", "updated_by", "afisha", "afisha_updated_at",
                                  "kassa", "kassa_updated_at"})
        self.assertEqual((r["ok"], r["role"], r["label"]), (True, "editor", "Организатор"))
        self.assertEqual(r["data"], DATA)
        self.assertEqual(r["afisha"], AFISHA)

    def test_load_viewer(self):
        _, r = req(self.s, op="load", code="kur-code")
        self.assertEqual((r["role"], r["label"]), ("viewer", "Куратор"))

    def test_bad_code(self):
        for code in ["nope", "", None, 5, "ORG-CODE", "org-code "]:
            _, r = req(self.s, op="load", code=code)
            self.assertEqual(r, {"ok": False, "error": "bad_code"}, code)
            _, r = req(self.s, op="save", code=code, data=DATA, since=None)
            self.assertEqual(r, {"ok": False, "error": "bad_code"}, code)

    def test_viewer_cannot_write(self):
        _, r = req(self.s, op="save", code="kur-code", data={"schools": []}, since=None)
        self.assertEqual(r, {"ok": False, "error": "read_only"})
        self.assertEqual(self.s.rows["akusha"]["data"], DATA)

    def test_save_ok_then_conflict(self):
        _, a = req(self.s, op="load", code="org-code")
        new = dict(DATA, plan={"e1": ["s1"]})
        _, r = req(self.s, op="save", code="org-code", data=new, since=a["updated_at"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["updated_by"], "Организатор")
        self.assertGreater(logic.parse_ts(r["updated_at"]), logic.parse_ts(a["updated_at"]))
        # второй редактор со старой отметкой получает конфликт и текущие данные
        _, c = req(self.s, op="save", code="org-code", data=DATA, since=a["updated_at"])
        self.assertEqual(c["error"], "conflict")
        self.assertEqual(c["data"], new)
        self.assertEqual(c["updated_at"], r["updated_at"])
        self.assertEqual(self.s.rows["akusha"]["data"], new)

    def test_save_without_since_overwrites(self):
        _, r = req(self.s, op="save", code="org-code", data={"schools": []}, since=None)
        self.assertTrue(r["ok"])

    def test_since_in_supabase_format(self):
        # отметки из Supabase приходят как ...+00:00 и с микросекундами
        _, r = req(self.s, op="save", code="org-code", data=DATA, since="2026-09-25T10:00:00.000001+00:00")
        self.assertTrue(r["ok"])
        _, r = req(self.s, op="save", code="org-code", data=DATA, since="2026-09-25T10:00:00Z")
        self.assertEqual(r["error"], "conflict")

    def test_poll_unchanged(self):
        _, a = req(self.s, op="load", code="kur-code")
        _, p = req(self.s, op="load", code="kur-code", since=a["updated_at"], afisha_since=a["afisha_updated_at"])
        self.assertTrue(p["ok"] and p["unchanged"])
        self.assertNotIn("data", p)
        req(self.s, op="save", code="org-code", data=DATA, since=None)
        _, p = req(self.s, op="load", code="kur-code", since=a["updated_at"], afisha_since=a["afisha_updated_at"])
        self.assertNotIn("unchanged", p)
        self.assertEqual(p["data"], DATA)

    def test_poll_afisha_changed(self):
        _, a = req(self.s, op="load", code="kur-code")
        self.s.put_state("events", AFISHA, "2026-09-28T08:00:00.000000+00:00", None)
        _, p = req(self.s, op="load", code="kur-code", since=a["updated_at"], afisha_since=a["afisha_updated_at"])
        self.assertEqual(p["afisha_updated_at"], "2026-09-28T08:00:00.000000+00:00")

    def test_kassa_in_load_and_poll(self):
        _, a = req(self.s, op="load", code="kur-code")
        self.assertIsNone(a["kassa"])
        # старый сайт не шлёт kassa_since — касса на «без изменений» не влияет
        self.s.put_state("kassa", {"events": [{"date": "2026-09-30", "sold": 29}]}, "2026-09-29T15:00:00.000000+00:00", "касса")
        _, p = req(self.s, op="load", code="kur-code", since=a["updated_at"], afisha_since=a["afisha_updated_at"])
        self.assertTrue(p.get("unchanged"))
        # новый сайт шлёт — видит, что касса обновилась
        _, p = req(self.s, op="load", code="kur-code", since=a["updated_at"], afisha_since=a["afisha_updated_at"], kassa_since=None)
        self.assertNotIn("unchanged", p)
        self.assertEqual(p["kassa"]["events"][0]["sold"], 29)
        _, p = req(self.s, op="load", code="kur-code", since=a["updated_at"], afisha_since=a["afisha_updated_at"],
                   kassa_since=p["kassa_updated_at"])
        self.assertTrue(p.get("unchanged"))

    def test_kassa_parse(self):
        import kassa
        page = ('<table><tr><td colspan="19">Событие (выбрать конкретное): Жизнь и быт горцев. Фольклорная программа / 2026-09-30 / 15:00 / Ставрополье / Активно / 6310289 / МБУК</td></tr>'
                '<tr><td colspan="19">Площадка: КДЦ Шукты / Махачкала / с. Шукты / 1746988</td></tr>'
                '<tr><td>300</td>' + '<td>1</td>' * 18 + '</tr>'
                '<tr class="report-data-total"><td></td><td>100</td><td>30000</td><td>71</td><td>21300</td><td>0</td><td>0</td><td>0</td><td>0</td>'
                '<td>29</td><td>8700</td><td>29</td><td>8700</td><td>0</td><td>0</td><td>29</td><td>8700</td><td>0</td><td>0</td></tr>'
                '<tr class="report-data-total"><td></td>' + '<td>999</td>' * 18 + '</tr></table>')
        ev = kassa.parse_sales(page)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["date"], ev[0]["time"], ev[0]["sold"], ev[0]["quota"], ev[0]["free"], ev[0]["id"]),
                         ("2026-09-30", "15:00", 29, 100, 71, 6310289))
        self.assertEqual(ev[0]["venue"], "КДЦ Шукты")

    def test_kassa_accounts(self):
        import kassa
        env = {"KASSIR_LOGIN": "a", "KASSIR_PASSWORD": "1", "KASSIR_LOGIN_BUTRI": "b", "KASSIR_PASSWORD_BUTRI": "2",
               "KASSIR_LOGIN_X": "c", "YDB_ENDPOINT": "e"}
        self.assertEqual(kassa.accounts(env), [("main", "a", "1"), ("butri", "b", "2")])

    def test_kassa_refresh(self):
        calls = []

        def refresh():
            calls.append(1)
            self.s.put_state("kassa", {"events": [{"sold": 34}]}, logic.now_iso(), "касса")
        _, r = req(self.s, op="kassa_refresh", code="nope")
        self.assertEqual(r["error"], "bad_code")
        _, r = logic.handle(self.s, json.dumps({"op": "kassa_refresh", "code": "kur-code"}), refresh)
        self.assertTrue(r["ok"] and r["refreshed"])
        self.assertEqual(r["kassa"]["events"][0]["sold"], 34)
        # второе нажатие сразу — кассу не дёргаем, отдаём то, что уже есть
        _, r = logic.handle(self.s, json.dumps({"op": "kassa_refresh", "code": "org-code"}), refresh)
        self.assertTrue(r["ok"])
        self.assertFalse(r["refreshed"])
        self.assertEqual(len(calls), 1)

    def test_kassa_refresh_error(self):
        _, r = logic.handle(self.s, json.dumps({"op": "kassa_refresh", "code": "org-code"}), lambda: "main: касса не ответила")
        self.assertEqual((r["ok"], r["error"], r["detail"]), (False, "kassa", "main: касса не ответила"))
        _, r = req(self.s, op="kassa_refresh", code="org-code")
        self.assertEqual(r["error"], "kassa")

    def test_kassa_push_merges_by_event(self):
        self.s.put_state("kassa", {"events": [{"id": 1, "date": "2026-10-16", "sold": 0}, {"id": 2, "date": "2026-10-17", "sold": 2}]},
                         "2026-10-08T05:31:36.000000+00:00", "касса")
        ev = [{"id": 2, "date": "2026-10-17", "time": "15:00", "venue": "КДЦ Шукты", "sold": 5, "quota": 100, "free": 95, "evil": "x"}]
        _, r = req(self.s, op="kassa_push", code="org-code", events=ev)
        self.assertTrue(r["ok"])
        k = self.s.rows["kassa"]["data"]
        got = {e["id"]: e for e in k["events"]}
        self.assertEqual(got[1]["sold"], 0)            # другая учётка не тронута
        self.assertEqual(got[2]["sold"], 5)
        self.assertNotIn("evil", got[2])
        self.assertEqual(k["source"], "закладка")
        self.assertEqual(self.s.rows["kassa"]["updated_at"], r["kassa_updated_at"])

    def test_kassa_push_rights_and_bad_data(self):
        ev = [{"id": 1, "date": "2026-10-16", "sold": 1}]
        self.assertEqual(req(self.s, op="kassa_push", code="kur-code", events=ev)[1]["error"], "read_only")
        self.assertEqual(req(self.s, op="kassa_push", code="nope", events=ev)[1]["error"], "bad_code")
        for bad in [[], "x", [{"id": "1", "date": "d"}], [{"date": "d"}], [1]]:
            st, r = req(self.s, op="kassa_push", code="org-code", events=bad)
            self.assertEqual((st, r["error"]), (400, "bad_request"), bad)

    def test_empty_base(self):
        s = MemStore([("org-code", "editor", "Организатор")])
        _, r = req(s, op="load", code="org-code")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["data"])
        self.assertEqual(r["afisha"], {})
        _, r = req(s, op="save", code="org-code", data=DATA, since=None)
        self.assertTrue(r["ok"])

    def test_bad_requests(self):
        for body in ["", "not json", "[]", '{"op":"drop"}', "x" * (logic.MAX_BODY + 1)]:
            st, r = logic.handle(self.s, body)
            self.assertEqual((st, r["error"]), (400, "bad_request"), body[:20])
        st, r = req(self.s, op="save", code="org-code", data=[1], since=None)
        self.assertEqual((st, r["error"]), (400, "bad_request"))

    def test_code_cache_skips_scrypt(self):
        req(self.s, op="load", code="kur-code")
        n = self.s.scans
        for _ in range(5):
            req(self.s, op="load", code="kur-code")
        self.assertEqual(self.s.scans, n)

    def test_code_cache_expires(self):
        req(self.s, op="load", code="kur-code")
        self.s.access = [a for a in self.s.access if a["role"] != "viewer"]
        key = next(iter(logic._known))
        found, at = logic._known[key]
        logic._known[key] = (found, at - logic.KNOWN_TTL - 1)
        _, r = req(self.s, op="load", code="kur-code")
        self.assertEqual(r["error"], "bad_code")


class HandlerTest(unittest.TestCase):
    def setUp(self):
        import index
        self.index = index
        logic.forget_codes()
        s = MemStore([("org-code", "editor", "Организатор")])
        s.put_state("akusha", DATA, "2026-09-25T10:00:00.000001+00:00", "x")
        index._store = s

    def test_cors_and_json(self):
        r = self.index.handler({"httpMethod": "POST", "body": json.dumps({"op": "load", "code": "org-code"})}, None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(r["headers"]["Access-Control-Allow-Origin"], "*")
        self.assertEqual(json.loads(r["body"])["data"], DATA)

    def test_options(self):
        r = self.index.handler({"httpMethod": "OPTIONS"}, None)
        self.assertEqual(r["statusCode"], 204)
        self.assertIn("Access-Control-Allow-Origin", r["headers"])

    def test_base64_body(self):
        import base64
        b = base64.b64encode(json.dumps({"op": "load", "code": "nope"}).encode()).decode()
        r = self.index.handler({"httpMethod": "POST", "body": b, "isBase64Encoded": True}, None)
        self.assertEqual(json.loads(r["body"])["error"], "bad_code")

    def test_store_failure_is_server_error(self):
        class Broken:
            def access_rows(self):
                raise RuntimeError("ydb down")
        self.index._store = Broken()
        r = self.index.handler({"httpMethod": "POST", "body": json.dumps({"op": "load", "code": "zzz"})}, None)
        self.assertEqual(r["statusCode"], 503)


if __name__ == "__main__":
    unittest.main()
