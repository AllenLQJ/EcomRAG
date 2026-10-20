"""纯标准库替身；不启动 Milvus/模型，不读取 dotenv。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.retrieval.embedder import FakeEmbedder
from app.retrieval.store import MilvusStore
from ingest.kb_common import smoke_check
from ingest.kb_switch import main as switch_main
from ingest.loaders import Document
from ingest.run import ingest_directory, main
from ingest.splitter import split_document
from ingest.versioning import VersionManager, deletion_plan
from test_ingest_retrieval import FakeDataType, Recorder


class Iterator:
    def __init__(self, rows):
        self.rows, self.closed = rows, False

    def next(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        self.closed = True


class Client:
    def __init__(self):
        self.rows, self.aliases, self.events = {}, {}, []
        self.connection = {"open": True}
        self.last_iterator = None
        self.fail_upsert = False
        self.bad_read = False

    def close(self):
        self.connection["open"] = False

    def require_connection(self):
        if not self.connection["open"]:
            raise ConnectionNotExistException("should create connection first")

    def has_collection(self, collection_name):
        return collection_name in self.rows  # 刻意模拟 alias 不被 has_collection 识别。

    def list_collections(self, **kwargs):
        self.require_connection()
        return list(self.rows)

    def describe_collection(self, collection_name, **kwargs):
        # 与真实 SDK 一致：返回含 fields 的 dict；测试集合不含 skus 字段。
        # 真实 Milvus 的 describe_collection 也接受 alias 名。
        target = self.aliases.get(collection_name, collection_name)
        assert target in self.rows
        return {"collection_name": target, "fields": [{"name": "chunk_id"}, {"name": "dense"}, {"name": "sparse"}]}

    def list_aliases(self, collection_name, **kwargs):
        return {"aliases": [alias for alias, target in self.aliases.items() if target == collection_name],
                "collection_name": collection_name, "db_name": "default"}

    def create_schema(self, **kwargs):
        return Recorder()

    def prepare_index_params(self):
        return Recorder()

    def create_collection(self, collection_name, **kwargs):
        assert collection_name not in self.aliases
        self.rows[collection_name] = {}
        self.events.append(("create", collection_name))

    def target(self, name):
        return self.aliases.get(name, name)

    def upsert(self, collection_name, data, **kwargs):
        if self.fail_upsert:
            self.fail_upsert = False
            raise RuntimeError("interrupted")
        self.rows[self.target(collection_name)].update({r["chunk_id"]: dict(r) for r in data})

    def flush(self, collection_name, **kwargs):
        self.events.append(("flush", collection_name))

    def load_collection(self, collection_name, **kwargs):
        self.events.append(("load", collection_name))

    def query_iterator(self, collection_name, output_fields, **kwargs):
        assert kwargs["consistency_level"] == "Strong"
        assert kwargs["filter"] is None
        rows = [{f: r[f] for f in output_fields} for r in self.rows[self.target(collection_name)].values()]
        if self.bad_read and rows:
            rows[0]["child_text"] = ""
        self.last_iterator = Iterator(rows)
        return self.last_iterator

    def create_alias(self, alias, collection_name, **kwargs):
        self.require_connection()
        assert alias not in self.rows and alias not in self.aliases
        self.aliases[alias] = collection_name
        self.events.append(("create_alias", collection_name))

    def alter_alias(self, alias, collection_name, **kwargs):
        self.require_connection()
        assert alias in self.aliases
        self.aliases[alias] = collection_name
        self.events.append(("alter_alias", collection_name))

    def rename_collection(self, old_name, new_name, **kwargs):
        self.rows[new_name] = self.rows.pop(old_name)
        self.events.append(("rename", old_name, new_name))

    def drop_collection(self, collection_name, **kwargs):
        del self.rows[collection_name]
        self.events.append(("drop", collection_name))

    def get_collection_stats(self, collection_name, **kwargs):
        return {"row_count": len(self.rows[collection_name])}

    def delete(self, collection_name, ids, **kwargs):
        self.events.append(("delete", list(ids)))
        for key in ids:
            self.rows[self.target(collection_name)].pop(key, None)


class ConnectionNotExistException(RuntimeError):
    """模拟 SDK 共享连接被任一客户端 close 后的异常。"""


def document(name="a.md", text="第一句。第二句。第三句。"):
    return Document(name, name, text, name, "product")


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = Client()
        self.store = MilvusStore(client=self.client, data_type=FakeDataType)
        self.manager = VersionManager(self.store, state_dir=self.tmp.name)
        self.chunks = split_document(document())
        self.embedder = FakeEmbedder()

    def build(self, version="kb_chunks_v1"):
        with self.manager.locked():
            return self.manager.build(self.chunks, 1, self.embedder, version=version)

    def test_alias_lookup_accepts_sdk_dict_and_legacy_list(self):
        self.client.rows = {"unrelated": {}, "kb_chunks_v1": {}}
        self.client.aliases["kb_chunks"] = "kb_chunks_v1"
        sdk_list_aliases = self.client.list_aliases
        for legacy in (False, True):
            def aliases(**kwargs):
                result = sdk_list_aliases(**kwargs)
                return result["aliases"] if legacy else result

            with self.subTest(legacy=legacy), patch.object(self.client, "list_aliases", side_effect=aliases) as rpc:
                self.assertEqual(self.manager.current(), "kb_chunks_v1")
                self.assertIsNone(self.store.alias_target("missing"))
                # dict 的元数据键不能被误认作真实 alias。
                self.assertIsNone(self.store.alias_target("aliases"))
                self.assertIsNone(self.store.alias_target("collection_name"))
                self.assertIsNone(self.store.alias_target("db_name"))
                self.assertTrue(all(call.kwargs["timeout"] == self.store.timeout_s for call in rpc.call_args_list))

    def test_alias_lookup_rejects_malformed_sdk_responses(self):
        self.client.rows["kb_chunks_v1"] = {}
        for response in ({}, {"aliases": None}, {"aliases": "kb_chunks"}, [123], None):
            with self.subTest(response=response), patch.object(self.client, "list_aliases", return_value=response):
                with self.assertRaises((KeyError, ValueError)):
                    self.manager.current()

    def test_alias_lookup_propagates_rpc_errors_instead_of_reporting_missing(self):
        self.client.rows["kb_chunks_v1"] = {}
        for operation in ("list_collections", "list_aliases"):
            for error in (TimeoutError("network timeout"), PermissionError("permission denied")):
                with self.subTest(operation=operation, error=error), \
                        patch.object(self.client, operation, side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        self.manager.current()
                    self.assertIs(caught.exception, error)

    def test_bind_alias_creates_missing_and_alters_existing_alias(self):
        self.build()
        self.build("kb_chunks_v2")
        self.manager.switch("kb_chunks_v1", yes=True)
        self.assertEqual(self.manager.current(), "kb_chunks_v1")
        self.manager.switch("kb_chunks_v2", yes=True)
        self.assertEqual(self.manager.current(), "kb_chunks_v2")
        alias_events = [event for event in self.client.events if event[0] in ("create_alias", "alter_alias")]
        self.assertEqual(alias_events, [("create_alias", "kb_chunks_v1"), ("alter_alias", "kb_chunks_v2")])

    def test_staged_build_never_switches_and_lists_stats(self):
        result = self.build()
        self.assertFalse(result["switched"])
        self.assertEqual(self.client.aliases, {})
        row = self.manager.listing()["versions"][0]
        self.assertEqual(row["entities"], len(self.chunks))
        self.assertIsNotNone(row["created_at"])
        self.assertTrue(self.client.last_iterator.closed)

    def test_validation_failure_cannot_switch(self):
        self.client.bad_read = True
        with self.assertRaisesRegex(ValueError, "必填"):
            self.build()
        with self.assertRaisesRegex(ValueError, "未完成"):
            self.manager.switch("kb_chunks_v1", yes=True)
        self.assertEqual(self.client.aliases, {})

    def test_dimension_count_and_content_are_checked_before_switch(self):
        self.build()
        rows = self.client.rows["kb_chunks_v1"]
        original = next(iter(rows.values()))
        for change in ({"dense": [1.0]}, {"child_text": "错误内容"}, {"doc_id": "different"}):
            saved = dict(original)
            original.update(change)
            with self.assertRaises(ValueError):
                self.manager.switch("kb_chunks_v1", yes=True)
            original.clear()
            original.update(saved)
        rows.clear()
        with self.assertRaisesRegex(ValueError, "统计"):
            self.manager.switch("kb_chunks_v1", yes=True)

    def test_atomic_switch_preserves_old_and_rollback(self):
        self.build()
        self.manager.switch("kb_chunks_v1", yes=True)
        self.build("kb_chunks_v2")
        self.manager.switch("kb_chunks_v2", yes=True)
        self.assertIn("kb_chunks_v1", self.client.rows)
        self.assertIn(("alter_alias", "kb_chunks_v2"), self.client.events)
        self.manager.switch("kb_chunks_v1", yes=True)
        self.assertEqual(self.manager.current(), "kb_chunks_v1")
        self.assertEqual(self.manager.protected(), {"kb_chunks_v1", "kb_chunks_v2"})

    def test_drop_protects_current_and_last_previous_after_rollback(self):
        for n in (1, 2, 3):
            name = f"kb_chunks_v{n}"
            self.build(name)
            self.manager.switch(name, yes=True)
        for name in ("kb_chunks_v3", "kb_chunks_v2"):
            with self.assertRaisesRegex(ValueError, "禁止删除"):
                self.manager.drop(name, yes=True)
        self.manager.drop("kb_chunks_v1")
        self.assertIn("kb_chunks_v1", self.client.rows)
        self.manager.drop("kb_chunks_v1", yes=True)
        self.assertNotIn("kb_chunks_v1", self.client.rows)

    def test_dry_commands_do_not_write(self):
        self.manager.switch("kb_chunks_v1")
        self.manager.migrate()
        self.assertEqual(self.client.events, [])
        self.assertFalse(self.manager.path.exists())

    def test_interrupted_build_resumes_idempotently(self):
        self.client.fail_upsert = True
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.build()
        self.build()
        self.build()
        self.assertEqual(len(self.client.rows["kb_chunks_v1"]), len(self.chunks))
        self.assertEqual(sum(e[0] == "create" for e in self.client.events), 1)

    def test_changed_input_needs_rebuild_and_rebuild_discards_stale_rows(self):
        self.build()
        self.chunks = split_document(document(text="新正文。"))
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.build()
        self.manager.build(self.chunks, 1, self.embedder, version="kb_chunks_v1", rebuild=True)
        self.assertIn(("drop", "kb_chunks_v1"), self.client.events)

    def test_cannot_rebuild_current_or_ever_activated_version(self):
        self.build()
        self.manager.switch("kb_chunks_v1", yes=True)
        with self.assertRaisesRegex(ValueError, "不能重写"):
            self.build()
        self.build("kb_chunks_v2")
        self.manager.switch("kb_chunks_v2", yes=True)
        with self.assertRaisesRegex(ValueError, "不能重写"):
            self.build()

    def test_old_deployment_reads_and_upserts_same_entity_without_migration(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        self.assertTrue(self.manager.listing()["legacy"])
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        self.assertEqual(len(self.client.rows["kb_chunks"]), len(self.chunks))
        self.assertEqual(self.client.aliases, {})
        self.build()
        with self.assertRaisesRegex(ValueError, "旧实体"):
            self.manager.switch("kb_chunks_v1", yes=True)

    def test_migrate_v0_and_alias_upsert_does_not_create_conflicting_entity(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        self.manager.migrate(yes=True)
        self.assertEqual(self.manager.current(), "kb_chunks_v0")
        store = MilvusStore(client=self.client, data_type=FakeDataType)
        store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        self.assertNotIn("kb_chunks", self.client.rows)
        self.manager.migrate(yes=True)
        self.assertEqual(sum(e[0] == "rename" for e in self.client.events), 1)

    def test_migration_can_resume_after_rename_before_alias_creation(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        with patch.object(self.client, "create_alias", side_effect=TimeoutError("interrupted")):
            with self.assertRaises(TimeoutError):
                self.manager.migrate(yes=True)
            self.assertNotIn("kb_chunks", self.client.rows)
            pending = json.loads(self.manager.path.read_text())["pending"]
            self.assertEqual(pending["phase"], "renamed_pending_alias")
            self.assertIsNone(pending["from"])
            self.assertIsNone(self.manager.current())
            # 再次失败仍须保留日志，不能因 None == None 将半成品吞掉。
            with self.assertRaises(TimeoutError):
                VersionManager(self.store, state_dir=self.tmp.name).recover()
            self.assertEqual(json.loads(self.manager.path.read_text())["pending"], pending)
        resumed = VersionManager(self.store, state_dir=self.tmp.name)
        resumed.migrate(yes=True)
        self.assertEqual(resumed.current(), "kb_chunks_v0")
        self.assertEqual(sum(e[0] == "rename" for e in self.client.events), 1)
        self.assertEqual(sum(e[0] == "create_alias" for e in self.client.events), 1)
        self.assertIsNone(resumed.state["pending"])
        self.assertEqual(len(resumed.state["history"]), 1)
        self.assertTrue(resumed.state["versions"]["kb_chunks_v0"]["activated"])

    def test_migration_resumes_if_process_exits_after_rename_before_saving_phase(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        rename = self.client.rename_collection

        def interrupted_rename(**kwargs):
            rename(**kwargs)
            raise SystemExit("process exited after rename")

        with patch.object(self.client, "rename_collection", side_effect=interrupted_rename):
            with self.assertRaises(SystemExit):
                self.manager.migrate(yes=True)
        self.assertEqual(json.loads(self.manager.path.read_text())["pending"]["phase"], "rename_pending")
        resumed = VersionManager(self.store, state_dir=self.tmp.name)
        resumed.migrate(yes=True)
        self.assertEqual(resumed.current(), "kb_chunks_v0")
        self.assertEqual(sum(e[0] == "rename" for e in self.client.events), 1)

    def test_migration_recovers_after_alias_rpc_before_recording_history(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        with patch.object(self.manager, "_record_switch", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.manager.migrate(yes=True)
        resumed = VersionManager(self.store, state_dir=self.tmp.name)
        resumed.migrate(yes=True)
        self.assertIsNone(resumed.state["pending"])
        self.assertEqual(len(resumed.state["history"]), 1)
        self.assertEqual(resumed.current(), "kb_chunks_v0")
        self.assertEqual(sum(e[0] == "create_alias" for e in self.client.events), 1)

    def test_legacy_migration_pending_is_recovered_instead_of_cleared(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        with patch.object(self.client, "create_alias", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.manager.migrate(yes=True)
        self.manager.state["pending"].pop("phase")
        self.manager.state["pending"].pop("operation")
        self.manager.save()
        resumed = VersionManager(self.store, state_dir=self.tmp.name)
        resumed.recover()
        self.assertEqual(resumed.current(), "kb_chunks_v0")
        self.assertEqual(len(resumed.state["history"]), 1)

    def test_migration_recovery_validates_rows_and_preserves_pending_on_conflict(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        with patch.object(self.client, "create_alias", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.manager.migrate(yes=True)
        pending = json.loads(self.manager.path.read_text())["pending"]
        self.client.bad_read = True
        with self.assertRaisesRegex(ValueError, "必填"):
            self.manager.recover()
        self.assertEqual(self.client.aliases, {})
        self.assertEqual(json.loads(self.manager.path.read_text())["pending"], pending)
        self.client.bad_read = False
        self.client.rows["kb_chunks"] = {}
        with self.assertRaisesRegex(ValueError, "kb_migrate --yes"):
            self.manager.recover()
        self.assertEqual(json.loads(self.manager.path.read_text())["pending"], pending)

    def test_recovery_keeps_pending_when_alias_lookup_fails(self):
        self.build()
        with patch.object(self.store, "bind_alias", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.manager.switch("kb_chunks_v1", yes=True)
        pending = json.loads(self.manager.path.read_text())["pending"]
        with patch.object(self.client, "list_aliases", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.manager.recover()
        self.assertEqual(json.loads(self.manager.path.read_text())["pending"], pending)
        # 普通新部署的 create_alias 未生效仍可撤销 pending，不冒充半成品迁移。
        self.manager.recover()
        self.assertIsNone(self.manager.state["pending"])

    def test_pending_switch_recovers_after_rpc_success_before_state_save(self):
        self.build()
        self.manager.switch("kb_chunks_v1", yes=True)
        self.build("kb_chunks_v2")
        with patch.object(self.manager, "_record_switch", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.manager.switch("kb_chunks_v2", yes=True)
        recovered = VersionManager(self.store, state_dir=self.tmp.name)
        recovered.recover()
        self.assertEqual(recovered.protected(), {"kb_chunks_v1", "kb_chunks_v2"})

    def test_switch_failure_before_rpc_success_preserves_old_alias(self):
        self.build()
        self.manager.switch("kb_chunks_v1", yes=True)
        self.build("kb_chunks_v2")
        with patch.object(self.store, "bind_alias", side_effect=RuntimeError("rpc failed")):
            with self.assertRaises(RuntimeError):
                self.manager.switch("kb_chunks_v2", yes=True)
        self.manager.recover()
        self.assertEqual(self.manager.current(), "kb_chunks_v1")

    def test_external_alias_change_blocks_drop(self):
        self.build()
        self.build("kb_chunks_v2")
        self.manager.switch("kb_chunks_v1", yes=True)
        self.client.aliases["kb_chunks"] = "kb_chunks_v2"
        with self.assertRaisesRegex(ValueError, "历史不一致"):
            self.manager.drop("kb_chunks_v1", yes=True)

    def test_check_low_hit5_rejects_without_changing_alias(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "Hit@5"):
            self.manager.switch("kb_chunks_v1", yes=True, check=lambda _: {"hit5": 0.89})
        self.assertIsNone(self.manager.current())
        self.manager.switch("kb_chunks_v1", yes=True, check=lambda _: {"hit5": 0.9})

    def test_cli_check_without_yes_does_not_switch(self):
        self.build()
        with patch("ingest.kb_common.smoke_check", return_value={"hit5": 1.0}), contextlib.redirect_stdout(io.StringIO()):
            result = switch_main(["--to", "kb_chunks_v1", "--check", "--state-dir", self.tmp.name],
                                 settings=SimpleNamespace(), store=self.store)
        self.assertEqual(result, 0)
        self.assertIsNone(self.manager.current())

    def test_cli_check_with_yes_rejects_low_hit5(self):
        self.build()
        with patch("ingest.kb_common.smoke_check", return_value={"hit5": 0.5}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = switch_main(["--to", "kb_chunks_v1", "--check", "--yes", "--state-dir", self.tmp.name],
                                 settings=SimpleNamespace(), store=self.store)
        self.assertEqual(result, 1)
        self.assertIsNone(self.manager.current())

    def test_cli_primary_gate_rejects_low_or_nonfinite_scores_before_switch(self):
        self.build()
        self.manager.switch("kb_chunks_v1", yes=True)
        self.build("kb_chunks_v2")
        events = list(self.client.events)
        for yes in ([], ["--yes"]):
            for primary in (0.0, 0.89, float("nan"), float("inf")):
                with self.subTest(yes=yes, primary=primary), \
                        patch("ingest.kb_common.smoke_check", return_value={"hit5": 1.0, "primary_hit5": primary}), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                    result = switch_main(["--to", "kb_chunks_v2", "--check", "--min-primary-hit5", "0.9",
                                          "--state-dir", self.tmp.name, *yes],
                                         settings=SimpleNamespace(), store=self.store)
                self.assertEqual(result, 1)
                self.assertIn("Primary Hit@5", errors.getvalue())
                self.assertEqual(self.manager.current(), "kb_chunks_v1")
        self.assertEqual([event for event in self.client.events if "alias" in event[0]],
                         [event for event in events if "alias" in event[0]])

    def test_cli_primary_gate_boundary_and_default_compatibility(self):
        for index, (threshold, primary) in enumerate(((None, 0.0), ("0.9", 0.9), ("0", 0.0))):
            for yes in ([], ["--yes"]):
                with self.subTest(threshold=threshold, yes=yes):
                    version = f"kb_chunks_v{index}{int(bool(yes))}"
                    self.build(version)
                    previous = self.manager.current()
                    args = ["--to", version, "--check", "--state-dir", self.tmp.name, *yes]
                    if threshold is not None:
                        args.extend(["--min-primary-hit5", threshold])
                    output = io.StringIO()
                    with patch("ingest.kb_common.smoke_check", return_value={"hit5": 1.0, "primary_hit5": primary}), \
                            contextlib.redirect_stdout(output):
                        result = switch_main(args, settings=SimpleNamespace(), store=self.store)
                    self.assertEqual(result, 0)
                    self.assertEqual(self.manager.current(), version if yes else previous)
                    self.assertEqual(json.loads(output.getvalue().splitlines()[0])["check"]["primary_hit5"], primary)

    def test_cli_primary_gate_requires_check_and_valid_threshold(self):
        for yes in ([], ["--yes"]):
            for threshold in ("-0.1", "1.1", "nan", "inf", "-inf"):
                with self.subTest(threshold=threshold, yes=yes), \
                        patch("ingest.kb_common.smoke_check") as check, \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    result = switch_main(["--to", "kb_chunks_v1", "--check",
                                          "--min-primary-hit5=" + threshold, "--state-dir", self.tmp.name, *yes],
                                         settings=SimpleNamespace(), store=self.store)
                self.assertEqual(result, 1)
                self.assertIn("min-primary-hit5", errors.getvalue())
                check.assert_not_called()
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                result = switch_main(["--to", "kb_chunks_v1", "--min-primary-hit5", "0.9",
                                      "--state-dir", self.tmp.name, *yes], settings=SimpleNamespace(), store=self.store)
            self.assertEqual(result, 1)
            self.assertIn("--check", errors.getvalue())
        self.assertIsNone(self.manager.current())

    def test_primary_gate_does_not_bypass_any_hit_gate(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "Hit@5"):
            self.manager.switch("kb_chunks_v1", yes=True, min_primary_hit5=0.9,
                                check=lambda _: {"hit5": 0.5, "primary_hit5": 1.0})
        self.assertIsNone(self.manager.current())

    def test_cli_check_yes_keeps_shared_connection_for_current_and_alias(self):
        self.build()
        self.build("kb_chunks_v2")
        peer = Client()
        peer.connection = self.client.connection
        # 证明替身能复现故障：不同客户端对象关闭同一连接后 current() 会抛错。
        peer.close()
        with self.assertRaises(ConnectionNotExistException):
            self.manager.current()
        self.client.connection["open"] = True
        path = Path(self.tmp.name) / "eval.jsonl"
        path.write_text(json.dumps({"question": "发票", "answerable": True,
                                    "expected_doc": ["original.md", "case.md"]}), encoding="utf-8")
        settings = SimpleNamespace(bge_m3_path="unused", embed_batch_size=1,
                                   milvus_uri=self.store.uri, milvus_timeout_s=10)
        fail_check = False

        def retriever(client, *args, **kwargs):
            self.assertIs(client, self.client)
            self.assertIn(kwargs["collection_name"], ("kb_chunks_v1", "kb_chunks_v2"))
            def retrieve(*args, **kwargs):
                client.require_connection()
                if fail_check:
                    raise RuntimeError("检索失败")
                return [SimpleNamespace(source="case.md", doc_id="case-id")]
            return SimpleNamespace(client=client, retrieve=retrieve)

        with patch("app.retrieval.embedder.BgeM3Embedder", return_value=self.embedder), \
                patch.dict(sys.modules, {"app.retrieval.retriever": SimpleNamespace(Retriever=retriever)}), \
                patch.object(self.client, "close", wraps=self.client.close) as close, \
                contextlib.redirect_stdout(io.StringIO()):
            for version in ("kb_chunks_v1", "kb_chunks_v2"):
                result = switch_main(["--to", version, "--check", "--yes", "--eval-file", str(path),
                                      "--state-dir", self.tmp.name], settings=settings, store=self.store)
                self.assertEqual(result, 0)
                self.assertEqual(self.manager.current(), version)
            fail_check = True
            with contextlib.redirect_stderr(io.StringIO()):
                result = switch_main(["--to", "kb_chunks_v1", "--check", "--yes", "--eval-file", str(path),
                                      "--state-dir", self.tmp.name], settings=settings, store=self.store)
            self.assertEqual(result, 1)
            self.assertEqual(self.manager.current(), "kb_chunks_v2")
            close.assert_not_called()
        self.assertEqual(self.store.collection_name, "kb_chunks")
        self.assertEqual([event[0] for event in self.client.events if "alias" in event[0]],
                         ["create_alias", "alter_alias"])

    def test_sync_delete_only_stale_chunks_in_scanned_document(self):
        directory = Path(self.tmp.name) / "docs"
        directory.mkdir()
        (directory / "a.md").write_text("# 标题\n" + "正文。" * 100, encoding="utf-8")
        ingest_directory(directory, dry_run=False, embedder=self.embedder, store=self.store,
                         split_options={"child_chunk_size": 50, "child_chunk_overlap": 0})
        other = split_document(document("other.md"))
        self.store.upsert(other, self.embedder.encode([c.child_text for c in other]))
        before = len(self.client.rows["kb_chunks"])
        (directory / "a.md").write_text("# 标题\n缩短正文。", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            result = ingest_directory(directory, dry_run=False, embedder=self.embedder, store=self.store,
                                      sync_delete=True, yes=True,
                                      split_options={"child_chunk_size": 50, "child_chunk_overlap": 0})
        self.assertTrue(result["delete_plan"])
        self.assertLess(len(self.client.rows["kb_chunks"]), before)
        self.assertIn(other[0].chunk_id, self.client.rows["kb_chunks"])
        with contextlib.redirect_stdout(io.StringIO()):
            result = ingest_directory(directory, dry_run=False, embedder=self.embedder, store=self.store,
                                      sync_delete=True, prune_missing=True, yes=True)
        self.assertNotIn(other[0].chunk_id, self.client.rows["kb_chunks"])

    def test_delete_plan_uses_exact_doc_source_pairs_and_primary_keys(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        plan = deletion_plan(self.store, [], [document("different.md")])
        self.assertEqual(plan, [])
        plan = deletion_plan(self.store, [], [document()])
        self.assertEqual([r["chunk_id"] for r in plan], [c.chunk_id for c in self.chunks])
        self.store.delete_ids([r["chunk_id"] for r in plan])
        self.assertIn(("delete", [c.chunk_id for c in self.chunks]), self.client.events)

    def test_sync_delete_requires_yes_before_write(self):
        with self.assertRaisesRegex(ValueError, "--yes"):
            ingest_directory(Path(__file__).parent / "fixtures", dry_run=False,
                             store=self.store, embedder=self.embedder, sync_delete=True)
        self.assertEqual(self.client.events, [])

    def test_cli_defaults_to_offline_dry_run(self):
        settings = SimpleNamespace(embed_batch_size=16, parent_max_chars=1200,
                                   child_chunk_size=300, child_chunk_overlap=50)
        with patch("socket.socket", side_effect=AssertionError("network")), contextlib.redirect_stdout(io.StringIO()) as output:
            result = main(["--dir", str(Path(__file__).parent / "fixtures")], settings=settings)
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])

    def test_staged_cli_yes_builds_but_never_switches(self):
        settings = SimpleNamespace(embed_batch_size=16, parent_max_chars=1200,
                                   child_chunk_size=300, child_chunk_overlap=50,
                                   milvus_uri=self.store.uri, milvus_timeout_s=10, bge_m3_path="unused")
        with patch("app.retrieval.store.MilvusStore", return_value=self.store), \
                patch.object(self.store, "for_collection", side_effect=lambda name: self.child_store(name)), \
                patch("app.retrieval.embedder.BgeM3Embedder", return_value=self.embedder), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = main(["--dir", str(Path(__file__).parent / "fixtures"), "--staged", "--yes",
                           "--version", "kb_chunks_v10", "--state-dir", self.tmp.name], settings=settings)
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output.getvalue())["switched"])
        self.assertEqual(self.client.aliases, {})

    def child_store(self, name):
        # 直接取原类，避免 CLI 构造器替身影响 for_collection 的实例化。
        return MilvusStore(collection_name=name, client=self.client, data_type=FakeDataType)

    def test_plan_cli_reads_exact_deletions_without_model_or_writes(self):
        self.store.upsert(self.chunks, self.embedder.encode([c.child_text for c in self.chunks]))
        settings = SimpleNamespace(embed_batch_size=16, parent_max_chars=1200,
                                   child_chunk_size=300, child_chunk_overlap=50,
                                   milvus_uri=self.store.uri, milvus_timeout_s=10)
        before = list(self.client.events)
        with patch("app.retrieval.store.MilvusStore", return_value=self.store), \
                patch("app.retrieval.embedder.BgeM3Embedder", side_effect=AssertionError("model")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = main(["--dir", str(Path(__file__).parent / "fixtures"), "--sync-delete",
                           "--prune-missing", "--plan"], settings=settings)
        self.assertEqual(result, 0)
        self.assertIn("delete_plan", output.getvalue())
        self.assertEqual(before, self.client.events)

    def test_smoke_filters_and_matches_exact_source(self):
        path = Path(self.tmp.name) / "eval.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in [
            {"answerable": False, "expected_doc": "x", "question": "skip"},
            {"answerable": True, "question": "skip"},
            {"answerable": True, "expected_doc": "a.md", "question": "问题"},
        ]), encoding="utf-8")
        retriever = SimpleNamespace(retrieve=lambda *a, **kw: [SimpleNamespace(source="a.md", doc_id="id")])
        report = smoke_check("kb_chunks_v1", None, eval_file=str(path), retriever=retriever)
        self.assertEqual((report["queries"], report["hit5"]), (1, 1.0))


if __name__ == "__main__":
    unittest.main()
