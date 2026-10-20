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
from datetime import datetime, timezone
from pathlib import Path

from ingest.loaders import PROJECT_ROOT
from ingest.splitter import chunk_record


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_check_thresholds(min_hit5, min_primary_hit5):
    for name, threshold in (("min-hit5", min_hit5), ("min-primary-hit5", min_primary_hit5)):
        if name == "min-primary-hit5" and threshold is None:
            continue
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError(f"{name} 必须在 [0, 1]")


def validate_check_report(report, min_hit5, min_primary_hit5):
    for key, label, threshold in (("hit5", "Hit@5", min_hit5),
                                  ("primary_hit5", "Primary Hit@5", min_primary_hit5)):
        if threshold is None:
            continue
        score = report[key]
        # NaN 与任何数比较都为假，必须先要求有限值。
        if not math.isfinite(score) or score < threshold:
            raise ValueError(f"{label}={score:.3f} 无效或低于阈值 {threshold}，拒绝切换")


def manifest(chunks, document_count):
    rows = {chunk.chunk_id: digest(chunk_record(chunk)) for chunk in chunks}
    if len(rows) != len(chunks) or not chunks:
        raise ValueError("构建必须有非空且主键唯一的块")
    stats = {"documents": document_count,
             "parent_chunks": len({c.parent_id for c in chunks}), "child_chunks": len(chunks)}
    return {"rows": rows, "stats": stats, "fingerprint": digest(rows),
            **({"skus_metadata": True} if any(c.skus is not None for c in chunks) else {})}


def validate(store, expected):
    from app.retrieval.store import TEXT_FIELDS
    from app.retrieval.embedder import DENSE_DIM

    found, docs, parents = {}, set(), set()
    options = {"fields": [*TEXT_FIELDS, "dense", "skus"]} if expected.get("skus_metadata") else {}
    for row in store.iter_rows(**options):
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
        content = {field: row[field] for field in TEXT_FIELDS}
        if expected.get("skus_metadata"):
            raw = row.get("skus")
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
                raise ValueError("构建校验失败：skus 必须为 JSON 字符串且不超长")
            parsed = json.loads(raw)
            from ingest.product_metadata import parse_skus
            if parsed is not None and (not isinstance(parsed, list) or parse_skus(parsed) != parsed):
                raise ValueError("构建校验失败：skus 必须为合法 SKU 数组或 null")
            if parsed is not None:
                content["skus"] = raw
        found[key] = digest(content)
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
        elif current is None and pending["from"] is None and (
            pending.get("operation") == "migrate" or (
                # 兼容旧状态文件：旧 migrate 在 rename 后才登记普通切换 pending。
                pending["to"] == self.alias + "_v0"
                and pending["to"] in self.store.collections()
                and not self.state["versions"][pending["to"]].get("activated")
            )
        ):
            self._recover_migration(pending)
        elif pending.get("operation", "switch") == "switch" and current == pending["from"]:
            # 仅普通 alias 切换可按原目标判定 RPC 未生效；迁移须按显式阶段恢复。
            self.state["pending"] = None
            self.save()
        else:
            raise ValueError("未完成切换与服务端状态冲突；需人工核对状态文件")

    def _recover_migration(self, pending):
        """rename 和 alias 创建之间没有事务；失败时保留明确阶段供下次重试。"""
        version = pending["to"]
        names = set(self.store.collections())
        if pending.get("phase") in (None, "rename_pending"):
            if self.alias in names and version not in names:
                self.store.rename(self.alias, version)
                names.remove(self.alias)
                names.add(version)
            elif self.alias in names or version not in names:
                raise ValueError("迁移集合状态冲突；请核对旧实体及 v0 后重跑 kb_migrate --yes")
            # 包括 rename RPC 成功、但保存阶段前进程退出的情况。
            pending.update(operation="migrate", phase="renamed_pending_alias")
            self.save()
        if pending["phase"] != "renamed_pending_alias" or self.alias in names or version not in names:
            raise ValueError("迁移集合状态冲突；请核对旧实体及 v0 后重跑 kb_migrate --yes")
        target = self.store.for_collection(version)
        target.flush_load()
        validate(target, self.state["versions"][version]["manifest"])
        self.state["versions"][version]["status"] = "ready"
        self.save()
        self.store.bind_alias(self.alias, version, exists=False)
        self._record_switch(pending)

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

    def switch(self, version, *, yes=False, check=None, min_hit5=0.9, min_primary_hit5=None):
        self.valid_version(version)
        validate_check_thresholds(min_hit5, min_primary_hit5)
        if min_primary_hit5 is not None and check is None:
            raise ValueError("min-primary-hit5 必须提供 check")
        if not yes:
            return {"dry_run": True, "to": version, "current": self.current()}
        self.recover()
        self.verify(version)
        if check is not None:
            report = check(version)
            validate_check_report(report, min_hit5, min_primary_hit5)
        previous = self.current()
        if previous == version:
            return {"current": version, "unchanged": True}
        if previous is None and self.alias in self.store.collections():
            raise ValueError("存在同名旧实体集合；请先执行 kb_migrate --yes")
        event = {"from": previous, "to": version, "at": now(), "operation": "switch"}
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
        # rename 前就登记写前日志，不能以 current == from == None 判定迁移未执行。
        event = {"from": None, "to": version, "at": now(),
                 "operation": "migrate", "phase": "rename_pending"}
        self.state["pending"] = event
        self.save()
        self.recover()
        return {"current": version, "previous": None, "switched": True}


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
