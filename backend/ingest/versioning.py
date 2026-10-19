"""发布控制面：本地持久化发布清单，Milvus SDK 操作全部委托 Store。

同一 URI/alias 必须共享 state_dir，并由一个发布者串行操作；fcntl 锁不替代分布式锁。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ingest.loaders import PROJECT_ROOT


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def manifest(chunks, document_count):
    rows = {chunk.chunk_id: digest(asdict(chunk)) for chunk in chunks}
    if len(rows) != len(chunks) or not chunks:
        raise ValueError("构建必须有非空且主键唯一的块")
    stats = {"documents": document_count,
             "parent_chunks": len({c.parent_id for c in chunks}), "child_chunks": len(chunks)}
    return {"rows": rows, "stats": stats, "fingerprint": digest(rows)}


def validate(store, expected):
    from app.retrieval.store import TEXT_FIELDS
    from app.retrieval.embedder import DENSE_DIM

    found, docs, parents = {}, set(), set()
    for row in store.iter_rows():
        for field, limit in TEXT_FIELDS.items():
            value = row.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
                raise ValueError(f"构建校验失败：必填字段 {field} 为空或超长")
        vector = row.get("dense", [])
        if len(vector) != DENSE_DIM or not all(math.isfinite(v) for v in vector):
            raise ValueError("构建校验失败：向量维度/数值错误")
        key = row["chunk_id"]
        if key in found:
            raise ValueError("构建校验失败：重复主键")
        found[key] = digest({field: row[field] for field in TEXT_FIELDS})
        docs.add(row["doc_id"])
        parents.add(row["parent_id"])
    stats = {"documents": len(docs), "parent_chunks": len(parents), "child_chunks": len(found)}
    if stats != expected["stats"] or found != expected["rows"]:
        raise ValueError("构建校验失败：文档/父块/子块统计或内容与 dry-run 清单不一致")
    return stats


class VersionManager:
    def __init__(self, store, *, alias="kb_chunks", state_dir=None):
        self.store, self.alias = store, alias
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
            raise ValueError("alias 必须是合法 Milvus 名称")
        key = digest({"uri": store.uri, "alias": alias})[:20]
        self.path = Path(state_dir or PROJECT_ROOT / ".kb-state") / (key + ".json")
        self.state = {"versions": {}, "history": [], "pending": None}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if self.path.exists():
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            try:
                yield self
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def valid_version(self, version):
        if not re.fullmatch(re.escape(self.alias) + r"_v[0-9][0-9A-Za-z_]*", version):
            raise ValueError("版本必须为 <alias>_v<时间戳或序号>")
        return version

    def current(self):
        return self.store.alias_target(self.alias)

    def recover(self):
        # 写前日志：alias RPC 成功、进程却在保存历史前退出时，恢复上一个回滚点。
        pending = self.state.get("pending")
        if not pending:
            return
        current = self.current()
        if current == pending["to"]:
            self._record_switch(pending)
        elif current == pending["from"]:
            self.state["pending"] = None
            self.save()
        else:
            raise ValueError("未完成切换与服务端状态冲突；需人工核对状态文件")

    def _record_switch(self, pending):
        self.state["history"].append(pending)
        self.state["versions"][pending["to"]]["activated"] = True
        self.state["pending"] = None
        self.save()

    def build(self, chunks, document_count, embedder, *, batch_size=16, version=None, rebuild=False):
        if batch_size <= 0:
            raise ValueError("编码批大小必须大于零")
        self.recover()
        version = self.valid_version(version or self.alias + "_v" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
        expected = manifest(chunks, document_count)
        old = self.state["versions"].get(version)
        if version == self.current() or (old and old.get("activated")):
            raise ValueError("不能重写当前或曾发布的版本；请使用新版本名")
        exists = version in self.store.collections()
        if exists and old is None:
            raise ValueError("不能接管没有发布清单的已有集合")
        if old and old["manifest"]["fingerprint"] != expected["fingerprint"] and not rebuild:
            raise ValueError("重试输入/切块参数已变化；请新建版本或 --rebuild")
        record = {"created_at": old["created_at"] if old else now(), "status": "building",
                  "manifest": expected, "activated": False}
        self.state["versions"][version] = record
        self.save()  # 在建集合之前登记，失败可重试；ready 也必须先失效。
        target = self.store.for_collection(version)
        if rebuild and exists:
            self.store.drop(version)
        target.ensure_collection()
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            target.upsert(batch, embedder.encode([c.child_text for c in batch]))
        target.flush_load()
        stats = validate(target, expected)
        record["status"] = "ready"
        self.save()
        return {"version": version, **stats, "switched": False}

    def verify(self, version):
        self.valid_version(version)
        record = self.state["versions"].get(version)
        if not record or record["status"] != "ready":
            raise ValueError("版本未完成构建校验，拒绝切换")
        target = self.store.for_collection(version)
        target.flush_load()
        return validate(target, record["manifest"])

    def switch(self, version, *, yes=False, check=None, min_hit5=0.9):
        self.valid_version(version)
        if not math.isfinite(min_hit5) or not 0 <= min_hit5 <= 1:
            raise ValueError("min-hit5 必须在 [0, 1]")
        if not yes:
            return {"dry_run": True, "to": version, "current": self.current()}
        self.recover()
        self.verify(version)
        if check is not None:
            report = check(version)
            if not math.isfinite(report["hit5"]) or report["hit5"] < min_hit5:
                raise ValueError(f"Hit@5={report['hit5']:.3f} 低于阈值 {min_hit5}，拒绝切换")
        previous = self.current()
        if previous == version:
            return {"current": version, "unchanged": True}
        if previous is None and self.alias in self.store.collections():
            raise ValueError("存在同名旧实体集合；请先执行 kb_migrate --yes")
        event = {"from": previous, "to": version, "at": now()}
        self.state["pending"] = event
        self.save()
        self.store.bind_alias(self.alias, version, exists=previous is not None)
        self._record_switch(event)
        # 保留旧版本加载状态以让在途请求完成；择期显式 release，不能在原子切换后立即释放。
        return {"current": version, "previous": previous, "switched": True}

    def protected(self):
        current = self.current()
        history = self.state["history"]
        previous = next((e["from"] for e in reversed(history)
                         if e["to"] == current and e["from"] and e["from"] != current), None)
        if (current and not history) or (history and history[-1]["to"] != current):
            raise ValueError("alias 与本地切换历史不一致，拒绝删除；需人工核对")
        return {name for name in (current, previous) if name}

    def drop(self, version, *, yes=False):
        self.valid_version(version)
        if self.state.get("pending"):
            if yes:
                self.recover()
            else:
                raise ValueError("存在未完成的发布操作，暂不能删除")
        if version in self.protected():
            raise ValueError("禁止删除当前版本或最近一个旧版本（回滚点）")
        if version not in self.state["versions"]:
            raise ValueError("拒绝删除未登记的集合")
        if not yes:
            return {"dry_run": True, "drop": version}
        if version in self.store.collections():
            self.store.drop(version)
        del self.state["versions"][version]
        self.save()
        return {"dropped": version}

    def listing(self):
        current = self.current()
        names = set(self.store.collections())
        return {"alias": self.alias, "current": current,
                "legacy": current is None and self.alias in names,
                "pending": self.state.get("pending"),
                "versions": [{"version": name, "entities": self.store.entity_count(name) if name in names else None,
                              "created_at": self.state["versions"].get(name, {}).get("created_at"),
                              "status": self.state["versions"].get(name, {}).get("status", "unmanaged")}
                             for name in sorted(names | set(self.state["versions"]))
                             if name == self.alias or re.fullmatch(re.escape(self.alias) + r"_v[0-9][0-9A-Za-z_]*", name)]}

    def migrate(self, *, yes=False):
        version = self.alias + "_v0"
        if not yes:
            return {"dry_run": True, "rename": self.alias, "to": version}
        self.recover()
        current = self.current()
        if current:
            return {"current": current, "unchanged": True}
        names = self.store.collections()
        if self.alias in names and version in names:
            raise ValueError("v0 已存在；拒绝覆盖")
        if self.alias not in names and version not in self.state["versions"]:
            raise ValueError("没有可迁移的旧集合")
        if version not in self.state["versions"]:
            # 读取旧行生成完整校验清单，不改动知识内容。
            from ingest.splitter import Chunk
            from app.retrieval.store import TEXT_FIELDS
            old = self.store.for_collection(self.alias)
            old.flush_load()
            rows = list(old.iter_rows())
            chunks = [Chunk(**{f: row[f] for f in TEXT_FIELDS}) for row in rows]
            expected = manifest(chunks, len({c.doc_id for c in chunks}))
            validate(old, expected)
            self.state["versions"][version] = {"created_at": now(), "status": "migrating",
                                                "manifest": expected, "activated": False}
            self.save()
        if self.alias in names:
            self.store.rename(self.alias, version)
        target = self.store.for_collection(version)
        target.flush_load()
        validate(target, self.state["versions"][version]["manifest"])
        self.state["versions"][version]["status"] = "ready"
        self.save()
        return self.switch(version, yes=True)


def deletion_plan(store, chunks, documents, *, prune_missing=False):
    keep = {c.chunk_id for c in chunks}
    scanned = {(d.doc_id, d.source) for d in documents}
    plan = []
    for row in store.iter_rows(fields=["chunk_id", "doc_id", "source"]):
        known = (row["doc_id"], row["source"]) in scanned
        if (known and row["chunk_id"] not in keep) or (prune_missing and not known):
            plan.append({"chunk_id": row["chunk_id"], "doc_id": row["doc_id"],
                         "source": row["source"], "reason": "stale" if known else "missing"})
    return sorted(plan, key=lambda row: row["chunk_id"])
