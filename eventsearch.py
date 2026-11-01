"""事件语义搜索：BAAI/bge-large-zh-v1.5 向量（SiliconFlow 免费模型）+ 余弦相似度。

策略：语义优先；向量不可用（无 key / 断网 / 接口失败）自动回退到
「关键词任意两字按序」的字面匹配，保证离线可用性。

向量存储：events.embed BLOB = 4 字节标题 CRC32 + float32 数组；
标题变了（CRC 不符）或没有向量的事件在搜索时惰性补算（一次批量调用）。
"""
import json
import sqlite3
import struct
import urllib.request
import zlib

from datetime import date

import db

API = "https://api.siliconflow.cn/v1/embeddings"
MODEL = "BAAI/bge-large-zh-v1.5"          # SiliconFlow 免费中文向量模型
THRESHOLD = 0.45                          # 余弦阈值：低于不展示
TOPK = 8


def embed_texts(texts):
    """批量向量化；任何失败返回 None（调用方走离线兜底）。"""
    key = db.get_config("siliconflow_key") or ""
    if not key or not texts:
        return None
    out = []
    try:
        for i in range(0, len(texts), 64):
            chunk = texts[i:i + 64]
            body = json.dumps({"model": MODEL, "input": chunk}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                API, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8"))
            data = sorted(resp["data"], key=lambda x: x["index"])
            out.extend(d["embedding"] for d in data)
    except Exception:
        return None
    return out


def _pack(text: str, vec) -> bytes:
    h = zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF
    return struct.pack(f"<I{len(vec)}f", h, *vec)


def _unpack(blob: bytes):
    n = (len(blob) - 4) // 4
    vals = struct.unpack(f"<I{n}f", blob)
    return vals[0], list(vals[1:])


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5 or 1.0
    nb = sum(x * x for x in b) ** 0.5 or 1.0
    return dot / (na * nb)


def _pair_like_rows(c, kw: str, today_iso: str):
    """离线兜底：关键词任意两字按序出现即命中。"""
    ps = [kw] + [kw[i] + kw[j] for i in range(len(kw)) for j in range(i + 1, len(kw))]
    ps = list(dict.fromkeys(ps))
    conds = " OR ".join("title LIKE ?" for _ in ps)
    args = [f"%{p}%" for p in ps]
    fut = [dict(r) for r in c.execute(
        f"SELECT id, date, start_time, end_time, title FROM events "
        f"WHERE ({conds}) AND date>=? ORDER BY date, start_time LIMIT ?", [*args, today_iso, 6])]
    past = [dict(r) for r in c.execute(
        f"SELECT id, date, start_time, end_time, title FROM events "
        f"WHERE ({conds}) AND date<? ORDER BY date DESC LIMIT ?", [*args, today_iso, 3])]
    return fut, past


def find(kw: str, today: date | None = None, limit: int = 8) -> dict:
    """事件搜索统一入口。返回 {results, past, engine}；engine=semantic/like/none。"""
    today = today or date.today()
    today_iso = today.isoformat()
    kw = (kw or "").strip()
    if not kw:
        return {"results": [], "past": [], "engine": "none"}

    with db.conn() as c:
        rows = []
        for r in c.execute("SELECT id, date, start_time, end_time, title, embed "
                           "FROM events ORDER BY date, start_time"):
            r = dict(r)
            blob = r.pop("embed", None)
            h = zlib.crc32(r["title"].encode("utf-8")) & 0xFFFFFFFF
            vec = None
            if blob:
                try:
                    th, vec = _unpack(blob)
                    if th != h:
                        vec = None                      # 标题改过，向量过期
                except struct.error:
                    vec = None
            r["_vec"] = vec
            rows.append(r)

    stale = [r for r in rows if not r["_vec"]]
    qres = embed_texts([kw] + [r["title"] for r in stale])
    if qres is None:                                    # 向量不可用 → 字面兜底
        with db.conn() as c:
            fut, past = _pair_like_rows(c, kw, today_iso)
        return {"results": fut, "past": past, "engine": "like"}

    qvec = qres[0]
    for r, v in zip(stale, qres[1:]):
        r["_vec"] = v
        try:
            with db.conn() as c:
                c.execute("UPDATE events SET embed=? WHERE id=?",
                          (_pack(r["title"], v), r["id"]))
        except sqlite3.Error:
            pass                                        # 向量缓存写失败不影响搜索

    scored = sorted(
        (( _cos(qvec, r["_vec"]), r) for r in rows if r["_vec"]),
        key=lambda x: (-x[0], x[1]["date"]))
    picked = [r for s, r in scored if s >= THRESHOLD][:TOPK]

    results = [r for r in picked if r["date"] >= today_iso][:limit]
    past = [r for r in picked if r["date"] < today_iso][:3]
    if len(results) + len(past) < 3:                    # 语义不足，字面匹配补齐
        with db.conn() as c:
            lf, lp = _pair_like_rows(c, kw, today_iso)
        seen = {r["id"] for r in results + past}
        results += [r for r in lf if r["id"] not in seen]
        past += [r for r in lp if r["id"] not in seen]
    return {"results": results, "past": past, "engine": "semantic"}
