import math
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.retrieval.embedder import BgeM3Embedder, EmbeddingBatch, FakeEmbedder, sparse_token_ids
from app.retrieval.store import MilvusStore, TEXT_FIELDS
from ingest.loaders import Document, load_document
from ingest.run import ingest_directory
from ingest.splitter import split_document

BACKEND = Path(__file__).resolve().parents[1]


class FakeDataType:
    VARCHAR = "VARCHAR"
    FLOAT_VECTOR = "FLOAT_VECTOR"
    SPARSE_FLOAT_VECTOR = "SPARSE_FLOAT_VECTOR"


class Recorder:
    def __init__(self):
        self.items = []

    def add_field(self, **kwargs):
        self.items.append(kwargs)

    def add_index(self, **kwargs):
        self.items.append(kwargs)


class FakeMilvusClient:
    def __init__(self):
        self.created = None
        self.rows = {}
        self.batches = []

    def has_collection(self, **kwargs):
        return self.created is not None

    def create_schema(self, **kwargs):
        self.schema_options = kwargs
        return Recorder()

    def prepare_index_params(self):
        return Recorder()

    def create_collection(self, **kwargs):
        self.created = kwargs

    def upsert(self, collection_name, data, timeout):
        self.upsert_timeout = timeout
        self.batches.append(len(data))
        self.rows.update({row["chunk_id"]: row for row in data})
        return {"upsert_count": len(data)}


class IndexingTests(unittest.TestCase):
    def test_modules_import_without_heavy_dependencies(self):
        code = '''import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"torch", "FlagEmbedding", "pymilvus", "pymysql", "docx", "pymupdf", "langchain_text_splitters", "pydantic_settings"}:
            raise AssertionError("重依赖不能在模块导入时加载：" + fullname)
sys.meta_path.insert(0, Block())
import app.retrieval.embedder, app.retrieval.store, ingest.run, ingest.load_faq, ingest.loaders
print("imports OK")
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("imports OK", result.stdout)

    def test_fake_embedding_shape_and_determinism(self):
        first = FakeEmbedder().encode(["退货说明", "商品参数"])
        self.assertEqual(first, FakeEmbedder().encode(["退货说明", "商品参数"]))
        self.assertNotEqual(first.dense[0], first.dense[1])
        for dense, sparse in zip(first.dense, first.sparse):
            self.assertEqual(len(dense), 1024)
            self.assertAlmostEqual(math.sqrt(sum(value * value for value in dense)), 1.0)
            self.assertTrue(sparse)

    def test_bge_one_call_returns_both_vectors_without_loading_model(self):
        calls = []

        class FakeModel:
            def encode(self, texts, **kwargs):
                calls.append((texts, kwargs))
                return {"dense_vecs": [[0.1] * 1024 for _ in texts],
                        "lexical_weights": [{"42": 0.7} for _ in texts]}

        factory = Mock(return_value=FakeModel())
        with patch.dict(os.environ), patch("app.retrieval.embedder.select_device", return_value="cpu"):
            embedder = BgeM3Embedder(BACKEND, model_factory=factory)
            self.assertIsNone(embedder._model)
            result = embedder.encode(["售后说明"])
        factory.assert_called_once_with(str(BACKEND.resolve()), devices="cpu", use_fp16=False)
        self.assertEqual(result.sparse, [{42: 0.7}])
        self.assertEqual(calls, [(["售后说明"], {"batch_size": 16, "return_dense": True,
                                             "return_sparse": True, "return_colbert_vecs": False})])

    def test_mps_and_cpu_selection_is_lazy_and_explicit(self):
        for available, device in [(True, "mps"), (False, "cpu")]:
            with self.subTest(device=device):
                torch = SimpleNamespace(backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: available)))
                factory = Mock(return_value=object())
                embedder = BgeM3Embedder(BACKEND, model_factory=factory)
                with patch.dict(os.environ), patch("app.retrieval.embedder.importlib.import_module", return_value=torch) as load:
                    embedder._load_model()
                load.assert_called_once_with("torch")
                factory.assert_called_once_with(str(BACKEND.resolve()), devices=device, use_fp16=False)

    def test_sparse_tokens_use_native_or_tokenizer_vocabulary_ids(self):
        tokenizer = Mock(unk_token_id=0, unk_token="<unk>")
        tokenizer.convert_tokens_to_ids.side_effect = lambda token: {"▁退货": 777}.get(token, 0)
        result = sparse_token_ids({"42": 0.7, "▁退货": 0.6, 99: 0.2}, tokenizer)
        self.assertEqual(result, {42: 0.7, 777: 0.6, 99: 0.2})
        tokenizer.convert_tokens_to_ids.assert_called_once_with("▁退货")
        with self.assertRaisesRegex(ValueError, "不在 tokenizer 词表"):
            sparse_token_ids({"未知词": 0.4}, tokenizer)
        with self.assertRaisesRegex(ValueError, "无法将稀疏 token"):
            sparse_token_ids({"没有 tokenizer 的词": 0.4})
        for weights in ({-1: 0.4}, {2**32 - 1: 0.4}, {1: float("nan")}, {1: -0.4}):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                sparse_token_ids(weights)

    def test_missing_local_model_never_downloads(self):
        with self.assertRaisesRegex(FileNotFoundError, "不会下载模型"):
            BgeM3Embedder(BACKEND / "missing-local-model").encode(["文本"])

    def test_schema_indexes_and_idempotent_upsert(self):
        client = FakeMilvusClient()
        store = MilvusStore(client=client, data_type=FakeDataType)
        chunks = split_document(Document("demo", "示例标题", "# 标题\n正文内容。", "product_demo.md", "product"))
        embeddings = FakeEmbedder().encode([chunk.child_text for chunk in chunks])
        store.upsert(chunks, embeddings)
        store.upsert(chunks, embeddings)
        self.assertEqual(len(client.rows), len(chunks))
        self.assertEqual(client.created["collection_name"], "kb_chunks")
        self.assertEqual(client.schema_options, {"auto_id": False, "enable_dynamic_field": False})
        fields = {field["field_name"]: field for field in client.created["schema"].items}
        self.assertEqual(set(fields), set(TEXT_FIELDS) | {"dense", "sparse"})
        for name, length in TEXT_FIELDS.items():
            self.assertEqual(fields[name]["max_length"], length)
            self.assertEqual(fields[name]["datatype"], "VARCHAR")
        self.assertTrue(fields["chunk_id"]["is_primary"])
        self.assertEqual(fields["dense"]["dim"], 1024)
        self.assertEqual(fields["dense"]["datatype"], "FLOAT_VECTOR")
        self.assertEqual(fields["sparse"]["datatype"], "SPARSE_FLOAT_VECTOR")
        indexes = {index["field_name"]: index for index in client.created["index_params"].items}
        self.assertEqual((indexes["dense"]["index_type"], indexes["dense"]["metric_type"]), ("HNSW", "COSINE"))
        self.assertEqual((indexes["sparse"]["index_type"], indexes["sparse"]["metric_type"]), ("SPARSE_INVERTED_INDEX", "IP"))
        row = client.rows[chunks[0].chunk_id]
        self.assertEqual(set(row), set(fields))
        self.assertEqual(row["parent_text"], chunks[0].parent_text)
        self.assertEqual(row["dense"], embeddings.dense[0])
        self.assertEqual(row["sparse"], embeddings.sparse[0])
        self.assertIsInstance(row["sparse"], dict)
        self.assertTrue(all(type(index) is int and type(weight) is float
                            for index, weight in row["sparse"].items()))

    def test_invalid_vectors_and_utf8_limits_fail_before_connect(self):
        chunk = split_document(Document("demo", "标题", "正文", "demo.md", "product"))[0]
        store = MilvusStore()
        with self.assertRaisesRegex(ValueError, "1024"):
            store.upsert([chunk], EmbeddingBatch([[1.0]], [{1: 1.0}]))
        too_long = replace(chunk, child_text="字" * 700)
        with self.assertRaisesRegex(ValueError, "child_text"):
            store.upsert([too_long], FakeEmbedder().encode([too_long.child_text]))

    def test_child_and_parent_utf8_byte_boundaries(self):
        chunk = split_document(Document("demo", "标题", "正文", "demo.md", "product"))[0]
        boundary = replace(chunk, child_text="字" * 682 + "ab", parent_text="字" * 2730 + "ab")
        self.assertEqual(len(boundary.child_text.encode("utf-8")), 2048)
        self.assertEqual(len(boundary.parent_text.encode("utf-8")), 8192)
        client = FakeMilvusClient()
        MilvusStore(client=client, data_type=FakeDataType).upsert(
            [boundary], FakeEmbedder().encode([boundary.child_text]))
        self.assertEqual(client.rows[boundary.chunk_id]["parent_text"], boundary.parent_text)
        for field in ("child_text", "parent_text"):
            oversized = replace(boundary, **{field: getattr(boundary, field) + "x"})
            store = MilvusStore()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                store.upsert([oversized], FakeEmbedder().encode([oversized.child_text]))
            self.assertIsNone(store.client)

    def test_full_fake_pipeline_batching(self):
        client = FakeMilvusClient()
        stats = ingest_directory(BACKEND.parent / "data/docs", dry_run=False,
                                 embedder=FakeEmbedder(),
                                 store=MilvusStore(client=client, data_type=FakeDataType))
        self.assertEqual(stats["documents"], 18)
        self.assertEqual(len(client.rows), stats["child_chunks"])
        self.assertTrue(all(size <= 16 for size in client.batches))
        self.assertIn(16, client.batches)

    def test_store_passes_timeout_to_connection_and_upsert(self):
        client = FakeMilvusClient()
        sdk = SimpleNamespace(MilvusClient=Mock(return_value=client), DataType=FakeDataType)
        store = MilvusStore("http://milvus.test:19530", timeout_s=2.5)
        with patch("app.retrieval.store.importlib.import_module", return_value=sdk):
            store._connect()
        sdk.MilvusClient.assert_called_once_with(uri="http://milvus.test:19530", timeout=2.5)
        chunks = split_document(load_document(BACKEND / "tests/fixtures/after_sales_policy.md"))
        store.upsert(chunks, FakeEmbedder().encode([chunk.child_text for chunk in chunks]))
        self.assertEqual(client.upsert_timeout, 2.5)
