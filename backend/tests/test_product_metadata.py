"""元数据链路用标准库与现有 Milvus/编码 fakes 验证，可完全离线运行。"""
import csv
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from app.retrieval.embedder import FakeEmbedder
from app.retrieval.store import MilvusStore
from ingest.loaders import Document, load_directory, load_document
from ingest.content_merge import merge_content
from ingest.product_metadata import ProductSourceIndex, parse_skus, source_skus
from ingest.run import ingest_directory
from ingest.splitter import chunk_record, split_document
from ingest.versioning import VersionManager, digest, manifest
from test_ingest_retrieval import FakeDataType, FakeMilvusClient
from test_ingest_versioning import Client


class ProductMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write_manifest(self, folder, rows):
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["file", "skus"])
            writer.writerows(rows)

    def test_parse_order_dedup_and_invalid_values(self):
        self.assertEqual(parse_skus("a-1；b-2 | a-1,../no ; x'"), ["a-1", "b-2"])
        self.assertEqual(parse_skus([None, "A-1", "a-1", "a-1"]), ["a-1"])
        self.assertEqual(parse_skus({"a-1": True}), [])

    def test_manifest_source_paths_filename_fallback_and_conflict(self):
        self.write_manifest(self.root / "one", [("guide.md", "a-1;b-2"), ("policy.md", "")])
        index = ProductSourceIndex([self.root])
        self.assertEqual(source_skus("folder/guide.md", index), ["a-1", "b-2"])
        self.assertEqual(source_skus(str(self.root / "one/guide.md"), index), ["a-1", "b-2"])
        self.assertEqual(source_skus("product_tinglan-s1.md", index), ["tinglan-s1"])
        self.assertEqual(source_skus("product_qiguang-l6.md", index), ["qiguang-l6"])
        for name in ("policy.md", "product_catalog.md", "product_guide_lamps.md", "logistics_a-1.md"):
            self.assertEqual(source_skus(name, index), [])
        self.write_manifest(self.root / "two", [("guide.md", "c-3")])
        index = ProductSourceIndex([self.root])
        self.assertEqual(source_skus("guide.md", index), [])
        self.assertEqual(source_skus(str(self.root / "one/guide.md"), index), ["a-1", "b-2"])

    def test_optional_loader_and_chunk_metadata_without_changing_ids(self):
        path = self.root / "product_guide.md"
        path.write_text("# 商品选购\n\n## 参数\n正文。", encoding="utf-8")
        self.write_manifest(self.root, [(path.name, "a-1;b-2")])
        old = load_directory(self.root)[0]
        new = load_directory(self.root, with_skus=True)[0]
        self.assertIsNone(old.skus)
        self.assertEqual(new.skus, ("a-1", "b-2"))
        self.assertEqual(old.doc_id, new.doc_id)
        chunks = split_document(new)
        self.assertTrue(all(c.skus == new.skus for c in chunks))
        self.assertEqual(chunks[0].chunk_id, split_document(old)[0].chunk_id)
        explicit = load_document(path, metadata={"skus": []})
        self.assertEqual(explicit.skus, ())

    def test_optional_merge_preserves_skus_for_flattened_ingest(self):
        data = self.root / "data"
        group = data / "docs_new/guides"
        self.write_manifest(group, [("product_guide.md", "a-1;b-2")])
        (group / "product_guide.md").write_text("# 选购\n\n## 说明\n正文。", encoding="utf-8")
        out = data / "docs_staging"
        merge_content(out, data / "faq_staging.csv", data_dir=data, with_skus=True)
        self.assertEqual(load_directory(out, with_skus=True)[0].skus, ("a-1", "b-2"))

    def test_default_schema_and_release_fingerprint_remain_compatible(self):
        chunk = split_document(Document("a", "标题", "内容。", "a.md", "product"))[0]
        previous = asdict(chunk)
        previous.pop("skus")
        self.assertEqual(chunk_record(chunk), previous)
        self.assertEqual(manifest([chunk], 1)["rows"][chunk.chunk_id], digest(previous))
        client = FakeMilvusClient()
        store = MilvusStore(client=client, data_type=FakeDataType)
        store.upsert([chunk], FakeEmbedder().encode([chunk.child_text]))
        self.assertNotIn("skus", next(iter(client.rows.values())))
        with self.assertRaisesRegex(ValueError, "显式启用"):
            store.upsert([replace(chunk, skus=("a-1",))], FakeEmbedder().encode([chunk.child_text]))

    def test_with_skus_staged_build_validates_and_switch_tools_can_read(self):
        self.write_manifest(self.root, [("guide.md", "a-1;b-2")])
        (self.root / "guide.md").write_text("# 选购指南\n\n## 选择\n正文。", encoding="utf-8")
        client = Client()
        store = MilvusStore(client=client, data_type=FakeDataType, skus_metadata=True)
        manager = VersionManager(store, state_dir=self.root / "state")
        result = ingest_directory(self.root, dry_run=False, staged=True, with_skus=True, yes=True,
                                  manager=manager, embedder=FakeEmbedder(), version="kb_chunks_v123")
        self.assertFalse(result["switched"])
        row = next(iter(client.rows["kb_chunks_v123"].values()))
        self.assertEqual(row["skus"], '["a-1","b-2"]')
        # switch/verify 创建默认 Store 时，按发布清单读取额外字段。
        default = VersionManager(MilvusStore(client=client, data_type=FakeDataType), state_dir=self.root / "state")
        self.assertEqual(default.verify("kb_chunks_v123")["documents"], 1)
        row["skus"] = '["other-3"]'
        with self.assertRaisesRegex(ValueError, "不一致"):
            default.verify("kb_chunks_v123")

    def test_new_schema_field_and_old_schema_refused_without_mutation(self):
        chunk = split_document(Document("a", "标题", "内容。", "a.md", "product", ("a-1",)))[0]
        client = FakeMilvusClient()
        store = MilvusStore(client=client, data_type=FakeDataType, skus_metadata=True)
        store.upsert([chunk], FakeEmbedder().encode([chunk.child_text]))
        fields = {field["field_name"]: field for field in client.created["schema"].items}
        self.assertEqual(fields["skus"]["max_length"], 4096)
        old = FakeMilvusClient()
        old.created = {"collection_name": "kb_chunks"}
        old.describe_collection = lambda **kw: {"fields": [{"name": "chunk_id"}]}
        store = MilvusStore(client=old, data_type=FakeDataType, skus_metadata=True)
        with self.assertRaisesRegex(ValueError, "已有集合缺少 skus"):
            store.upsert([chunk], FakeEmbedder().encode([chunk.child_text]))
        self.assertEqual(old.rows, {})

    def test_store_without_metadata_refuses_skus_schema_before_upsert(self):
        chunk = split_document(Document("a", "标题", "内容。", "a.md", "product"))[0]
        for alias in (False, True):
            with self.subTest(alias=alias):
                client = FakeMilvusClient()
                client.created = {"collection_name": "kb_chunks_v1" if alias else "kb_chunks"}
                client.has_collection = lambda **kw: not alias
                client.list_aliases = lambda **kw: {"aliases": ["kb_chunks"] if alias else []}
                client.describe_collection = lambda **kw: {"fields": [{"name": "skus"}]}
                store = MilvusStore(client=client, data_type=FakeDataType)
                with self.assertRaisesRegex(ValueError, "staged 构建并带 --with-skus"):
                    store.upsert([chunk], FakeEmbedder().encode([chunk.child_text]))
                self.assertEqual(client.rows, {})
                self.assertEqual(client.batches, [])
                self.assertFalse(store._ready)

    def test_store_existing_matching_schemas_can_upsert(self):
        base = split_document(Document("a", "标题", "内容。", "a.md", "product"))[0]
        for with_skus in (False, True):
            with self.subTest(with_skus=with_skus):
                chunk = replace(base, skus=("a-1",)) if with_skus else base
                client = FakeMilvusClient()
                client.created = {"collection_name": "kb_chunks"}
                fields = [{"name": "chunk_id"}] + ([{"name": "skus"}] if with_skus else [])
                client.describe_collection = lambda **kw: {"fields": fields}
                store = MilvusStore(client=client, data_type=FakeDataType, skus_metadata=with_skus)
                store.upsert([chunk], FakeEmbedder().encode([chunk.child_text]))
                row = client.rows[chunk.chunk_id]
                self.assertEqual(row.get("skus"), '["a-1"]' if with_skus else None)
