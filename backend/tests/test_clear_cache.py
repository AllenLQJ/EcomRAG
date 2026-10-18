import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.faq.cache import FaqCache, clear_cache
from ingest import clear_cache as cli


class BatchRedis:
    def __init__(self):
        self.deleted = []
        self.scans = []

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        if kwargs["cursor"] == 0:
            return 7, [f"faq:cache:{i}" for i in range(5)]
        return 0, ["faq:cache:5"]

    def delete(self, *keys):
        self.deleted.append(keys)
        return len(keys)


class ClearCacheTests(unittest.TestCase):
    def test_scan_and_delete_are_batched_and_prefix_is_restricted(self):
        client = BatchRedis()
        self.assertEqual(clear_cache(client, batch_size=2), 6)
        self.assertEqual([len(keys) for keys in client.deleted], [2, 2, 1, 1])
        self.assertEqual([call["cursor"] for call in client.scans], [0, 7])
        self.assertTrue(all(call["match"] == "faq:cache:*" for call in client.scans))
        self.assertTrue(all(call["count"] == 2 for call in client.scans))

    def test_clear_no_keys_and_invalid_size(self):
        client = Mock()
        client.scan.return_value = (0, [])
        self.assertEqual(clear_cache(client), 0)
        client.delete.assert_not_called()
        with self.assertRaises(ValueError):
            clear_cache(client, batch_size=0)

    def test_cache_clear_and_delete_outage_only_warn(self):
        cache = FaqCache()
        cache._client = Mock(side_effect=ConnectionError("SECRET"))
        with self.assertLogs("app.faq.cache", "WARNING") as logs:
            self.assertEqual(cache.clear(), 0)
            cache.delete("PRIVATE_Q")
        self.assertNotIn("SECRET", "".join(logs.output))
        self.assertNotIn("PRIVATE_Q", "".join(logs.output))

    def test_clear_cli_summary_and_failure(self):
        settings = SimpleNamespace(redis_url="redis://fake")
        client = BatchRedis()
        with patch.object(FaqCache, "_client", return_value=client), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main([], settings=settings, client=client), 0)
        self.assertIn("6 条", out.getvalue())
        with patch.object(FaqCache, "_client", side_effect=ConnectionError("SECRET")), contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(cli.main([], settings=settings, client=client), 1)
        self.assertIn("Redis 不可用", out.getvalue())
        self.assertNotIn("SECRET", out.getvalue())
