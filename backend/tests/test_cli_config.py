import sys
from contextlib import nullcontext
from types import ModuleType
from unittest.mock import Mock

import pytest

from app import config
from app.retrieval import embedder, store
from ingest import faq_admin, load_faq, run, versioning
from ingest.loaders import resolve_data_path


@pytest.mark.parametrize("mode", ["faq", "documents", "dry_run"])
@pytest.mark.parametrize("injected", [False, True])
def test_cli_uses_config_loader_unless_settings_are_injected(monkeypatch, mode, injected):
    # 显式禁用环境文件；所有配置值均为本地测试值，不连接真实服务。
    settings = config.Settings(
        _env_file=None,
        mysql_host="127.0.0.1", mysql_port=3307, mysql_database="test_qimu",
        mysql_user="test_user", mysql_password="PLACEHOLDER_NOT_A_REAL_PASSWORD",
        milvus_uri="http://127.0.0.1:19531", bge_m3_path="models/PLACEHOLDER_LOCAL_MODEL",
        embed_batch_size=7, parent_max_chars=1000, child_chunk_size=280, child_chunk_overlap=40,
    )
    getter = Mock(side_effect=AssertionError("注入配置后不得读取 .env")) if injected else Mock(return_value=settings)
    if mode == "faq":
        monkeypatch.setattr(config, "get_settings", getter)
    else:
        monkeypatch.setattr(run, "cli_settings", getter)
    kwargs = {"settings": settings} if injected else {}

    if mode == "faq":
        connection = Mock()
        driver = ModuleType("pymysql")
        driver.connect = Mock(return_value=connection)
        monkeypatch.setitem(sys.modules, "pymysql", driver)
        importer = Mock(return_value=True)
        notifier = Mock(return_value=0)
        monkeypatch.setattr(faq_admin, "import_rows", importer)
        monkeypatch.setattr(faq_admin, "notify", notifier)
        assert load_faq.main(["data/faq.csv"], **kwargs) == 0
        driver.connect.assert_called_once_with(
            host="127.0.0.1", port=3307, user="test_user",
            password=settings.mysql_password.get_secret_value(), database="test_qimu",
            charset="utf8mb4", autocommit=False, connect_timeout=10,
            read_timeout=10, write_timeout=10,
        )
        assert importer.call_args.args[0] is connection
        assert len(importer.call_args.args[1]) == 128
        assert importer.call_args.kwargs == dict(dry_run=False, prune=False, yes=False, ignored=0,
                                                 backup_dir=None)
        notifier.assert_called_once_with(settings, client=None)
        connection.close.assert_called_once_with()
    else:
        encoder = Mock()
        client = Mock()
        monkeypatch.setattr(embedder, "BgeM3Embedder", encoder)
        monkeypatch.setattr(store, "MilvusStore", client)
        manager = Mock()
        manager.current.return_value = None
        manager.locked.side_effect = nullcontext
        monkeypatch.setattr(versioning, "VersionManager", Mock(return_value=manager))
        # 这里只验证参数传递，统计值为替身；真实默认切块基准见 test_ingest_run.py。
        ingest = Mock(return_value={"documents": 156, "parent_chunks": 584, "child_chunks": 1000})
        monkeypatch.setattr(run, "ingest_directory", ingest)
        args = ["--dir", "data/docs"] + (["--dry-run"] if mode == "dry_run" else ["--yes"])
        assert run.main(args, **kwargs) == 0
        expected = {
            "dry_run": mode == "dry_run", "batch_size": 7,
            "split_options": {"parent_max_chars": 1000, "child_chunk_size": 280, "child_chunk_overlap": 40},
        }
        if mode == "documents":
            encoder.assert_called_once_with(resolve_data_path(settings.bge_m3_path), 7)
            client.assert_called_once_with(settings.milvus_uri, timeout_s=settings.milvus_timeout_s)
            expected.update(embedder=encoder.return_value, store=client.return_value)
        else:
            encoder.assert_not_called()
            client.assert_not_called()
        ingest.assert_called_once_with(resolve_data_path("data/docs"), **expected)

    if injected:
        getter.assert_not_called()
    else:
        getter.assert_called_once_with()
