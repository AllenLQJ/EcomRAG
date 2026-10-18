import subprocess
import sys
import unittest
from pathlib import Path


class OnlineImportTests(unittest.TestCase):
    def test_imports_without_optional_dependencies_env_reads_or_network(self):
        code = '''import importlib.abc, os, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"torch", "FlagEmbedding", "pymilvus", "pymysql", "redis", "rank_bm25", "jieba"}:
            raise AssertionError("禁止导入可选依赖：" + fullname)
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        name = os.path.basename(os.fsdecode(args[0]))
        if name == ".env" or name.startswith(".env."):
            raise AssertionError("禁止读取环境文件")
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise AssertionError("禁止网络访问")
sys.meta_path.insert(0, Block())
sys.addaudithook(audit)
import app.main, app.config, app.contracts, app.pipeline
import app.faq.repo, app.faq.cache, app.faq.bm25, app.faq.service
import app.retrieval.embedder, app.retrieval.store, app.retrieval.retriever, app.retrieval.reranker
import eval.search_demo, eval.faq_demo, eval.check_retrieval
import app.services, app.rate_limit, app.llm.client, app.llm.router, app.llm.generator
import eval.chat_demo, eval.run_eval
import eval.llm_ping, app.llm.diagnostics
import eval.profile_rerank, eval.metrics
import app.budget, app.llm.suspicious
import app.chat_log, app.logging_config
import ingest.clear_cache, ingest.migrate_db, ingest.purge_chat_logs, eval.chat_logs_view
print("imports OK")
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("imports OK", result.stdout)
