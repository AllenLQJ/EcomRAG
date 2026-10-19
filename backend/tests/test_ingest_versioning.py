"""纯标准库替身；不启动 Milvus/模型，不读取 dotenv。"""
import contextlib
import io
import json
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
        self.last_iterator = None
        self.fail_upsert = False
        self.bad_read = False

    def has_collection(self, collection_name):
        return collection_name in self.rows  # 刻意模拟 alias 不被 has_collection 识别。

    def list_collections(self, **kwargs):
        return list(self.rows)

    def list_aliases(self, collection_name, **kwargs):
        return [alias for alias, target in self.aliases.items() if target == collection_name]

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
        rows = [{f: r[f] for f in output_fields} for r in self.rows[self.target(collection_name)].values()]
        if self.bad_read and rows:
            rows[0]["child_text"] = ""
        self.last_iterator = Iterator(rows)
        return self.last_iterator

    def create_alias(self, alias, collection_name, **kwargs):
        assert alias not in self.rows and alias not in self.aliases
        self.aliases[alias] = collection_name
        self.events.append(("create_alias", collection_name))

    def alter_alias(self, alias, collection_name, **kwargs):
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
        with patch.object(self.store, "bind_alias", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.manager.migrate(yes=True)
        resumed = VersionManager(self.store, state_dir=self.tmp.name)
        resumed.migrate(yes=True)
        self.assertEqual(resumed.current(), "kb_chunks_v0")
        self.assertEqual(sum(e[0] == "rename" for e in self.client.events), 1)

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
