"""Cloud Function `afisha-kassa`: раз в полчаса берёт продажи из кабинета kassir.ru.

Заходит на new-report.kassir.ru под учёткой организатора (KASSIR_LOGIN / KASSIR_PASSWORD
в переменных функции), заказывает один «Отчёт по продажам (организатор)» сразу по всем
событиям и записывает в YDB строку state `kassa`:
{"events":[{"id","name","date","time","state","quota","free","reserved","sold","returned"}], "at"}.
Сайт сам сопоставляет события с афишей по дате и времени.
"""
import html
import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request

BASE = "https://new-report.kassir.ru"
F = "CommonOrganizerForm"


class Kassir:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path, data=None, ajax=False):
        h = {"User-Agent": "Mozilla/5.0 (afisha-akusha)", "Accept-Language": "ru"}
        if ajax:
            h["X-Requested-With"] = "XMLHttpRequest"
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        with self.op.open(urllib.request.Request(BASE + path, data=body, headers=h), timeout=30) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    @staticmethod
    def csrf(page):
        m = re.search(r'name="_csrf-report" value="([^"]*)"', page)
        if not m:
            raise RuntimeError("на странице нет csrf")
        return m.group(1)

    def login(self, user, password):
        _, page = self.get("/user/security/login")
        url, page = self.get("/user/security/login", [
            ("_csrf-report", self.csrf(page)), ("login-form[username]", user),
            ("login-form[password]", password), ("login-form[rememberMe]", "0")])
        if "/user/security/login" in url or "login-form-password" in page:
            raise RuntimeError("касса не приняла логин или пароль")

    def events(self):
        _, t = self.get("/search/event/index", ajax=True)
        return json.loads(t)["data"]["rows"]

    def sales_report(self, event_ids):
        _, page = self.get("/report/request/create?report_id=1")
        form = [("_csrf-report", self.csrf(page)), (F + "[attribute_report_db]", "coreDb"),
                (F + "[attribute_event_id]", "")]
        form += [(F + "[attribute_event_id][]", str(i)) for i in event_ids]
        for k in ("agreement_contractor_id", "agreement_id", "organizer_id", "event_state",
                  "event_start_date_start", "event_start_date_end", "ticket_history_create_date_start",
                  "ticket_history_create_date_end", "user_id", "cash_office_id", "yandex_user_id"):
            form.append((F + "[attribute_%s]" % k, ""))
        form += [(F + "[attribute_event_state][]", s) for s in ("ACTIVE", "CLOSED", "SUSPENDED", "CANCELLED")]
        for k, v in (("agreement_include_sub", "0"), ("expand_external_event", "0"), ("expand_return", "0"),
                     ("filter_none_zero", "0"), ("ignore_discount_local", "1"), ("group_agreement", "0"),
                     ("group_sector", "0"), ("format_id", "11")):
            form.append((F + "[attribute_%s]" % k, v))
        _, page = self.get("/report/request/create?report_id=1", form)
        m = re.search(r"queue/view\?id=(\d+)", page)
        if not m:
            raise RuntimeError("касса не приняла запрос отчёта: %s" % re.findall(r'invalid-feedback">([^<]+)', page))
        rid = m.group(1)
        for _ in range(40):
            _, t = self.get("/report/queue/status?id=" + rid, ajax=True)
            req = json.loads(t)["data"]["request"]
            if req["status"] == 3:
                return self.get(req["downloadUri"])[1]
            if req["status"] not in (1, 2):
                raise RuntimeError("отчёт не сформирован: " + str(req.get("statusName")))
            time.sleep(2)
        raise RuntimeError("отчёт формируется слишком долго")


def _cells(tr):
    return [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]


EV_RE = re.compile(r"Событие[^:]*:\s*(.+?) / (\d{4}-\d{2}-\d{2}) / (\d{1,2}:\d{2}) / [^/]* / ([^/]+?) / (\d+) /")


def parse_sales(page):
    """Итоговая строка по каждому событию: квота, свободно, бронь, продано, возвраты."""
    out, cur = [], None
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", page, re.S):
        text = " ".join(_cells(tr))
        m = EV_RE.search(text)
        if m:
            cur = {"name": m.group(1).strip(), "date": m.group(2), "time": m.group(3),
                   "state": m.group(4).strip(), "id": int(m.group(5))}
            continue
        if cur and 'report-data-total' in tr[:80]:
            c = _cells(tr)
            n = [int(x) if x.lstrip("-").isdigit() else 0 for x in c[1:]]
            if len(n) >= 18:
                cur.update(quota=n[0], free=n[2], reserved=n[6], sold=n[8], returned=n[16])
                out.append(cur)
            cur = None
    return out


def collect(user, password):
    k = Kassir()
    k.login(user, password)
    ids = [r["id"] for r in k.events()]
    return parse_sales(k.sales_report(ids)) if ids else []


def handler(event, context):
    import index
    import logic
    user, password = os.environ.get("KASSIR_LOGIN"), os.environ.get("KASSIR_PASSWORD")
    if not user or not password:
        return {"statusCode": 200, "body": json.dumps({"ok": False, "error": "в функции не заданы KASSIR_LOGIN и KASSIR_PASSWORD"}, ensure_ascii=False)}
    events = collect(user, password)
    st = index.store()
    st.in_tx(lambda tx: tx.put_state("kassa", {"events": events, "at": logic.now_iso()}, logic.now_iso(), "касса"))
    return {"statusCode": 200, "body": json.dumps({"ok": True, "events": len(events),
                                                   "sold": sum(e.get("sold", 0) for e in events)})}
