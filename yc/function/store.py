"""Хранилище в YDB serverless. Таблицы описаны в yc/schema.yql.

state.data лежит текстом JSON как есть — так копия из Supabase совпадает
побайтно, а разбор идёт только при отдаче.
"""
import json

import ydb

Q_ACCESS = "SELECT salt, hash, role, label FROM access;"

Q_META = "SELECT id, updated_at FROM meta;"

Q_STATE = """
DECLARE $id AS Utf8;
SELECT data, updated_at, updated_by FROM state WHERE id = $id;
"""

Q_PUT = """
DECLARE $id AS Utf8;
DECLARE $data AS Utf8;
DECLARE $at AS Utf8;
DECLARE $by AS Utf8;
UPSERT INTO state (id, data, updated_at, updated_by) VALUES ($id, $data, $at, $by);
UPSERT INTO meta (id, updated_at) VALUES ($id, $at);
"""


def _u(v):
    return (v, ydb.PrimitiveType.Utf8)


def _rows(result_sets):
    out = []
    for rs in result_sets:
        out.extend(rs.rows)
    return out


def _state_row(rows):
    if not rows:
        return None
    r = rows[0]
    return {"data": json.loads(r["data"]) if r["data"] is not None else None,
            "updated_at": r["updated_at"], "updated_by": r["updated_by"]}


def dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class _Tx:
    def __init__(self, tx):
        self.tx = tx

    def _run(self, q, params):
        with self.tx.execute(q, params, commit_tx=False) as results:
            return _rows(results)

    def state(self, sid):
        return _state_row(self._run(Q_STATE, {"$id": _u(sid)}))

    def put_state(self, sid, data, at, by):
        self._run(Q_PUT, {"$id": _u(sid), "$data": _u(dump(data)), "$at": _u(at), "$by": _u(by)})


class YdbStore:
    def __init__(self, endpoint, database, credentials):
        self.driver = ydb.Driver(endpoint=endpoint, database=database, credentials=credentials)
        self.driver.wait(timeout=8, fail_fast=True)
        self.pool = ydb.QuerySessionPool(self.driver)

    def _q(self, q, params=None):
        return _rows(self.pool.execute_with_retries(q, params))

    def access_rows(self):
        return [{"salt": bytes(r["salt"]), "hash": bytes(r["hash"]), "role": r["role"], "label": r["label"]}
                for r in self._q(Q_ACCESS)]

    def meta(self):
        return {r["id"]: r["updated_at"] for r in self._q(Q_META)}

    def state(self, sid):
        return _state_row(self._q(Q_STATE, {"$id": _u(sid)}))

    def in_tx(self, fn):
        """fn(tx) в сериализуемой транзакции; при конфликте YDB повторяется целиком."""
        def callee(session):
            with session.transaction(ydb.QuerySerializableReadWrite()) as tx:
                res = fn(_Tx(tx))
                tx.commit()
                return res
        return self.pool.retry_operation_sync(callee)
