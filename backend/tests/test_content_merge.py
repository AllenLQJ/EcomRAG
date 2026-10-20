import csv
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ingest import content_merge as merge
from ingest.run import ingest_directory


class ContentMergeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve() / "data"
        self.out = self.data / "docs_staging"
        self.faq_out = self.data / "faq_staging.csv"
        self.write("docs/product_catalog.md", "# 旧目录\n\n## 目录\n旧内容。")
        self.write("docs/after_sales_warranty.md", "# 旧保修\n\n旧保障。")
        self.write("docs/logistics_keep.md", "# 配送\n\n## 范围\n保留配送说明。")
        self.write("docs_generated/product_catalog.md", "# 新目录\n\n## 目录\n新内容。")
        self.write("docs_generated/after_sales_warranty.md", "# 新保修\n\n新保障。")
        self.write("docs_generated/product_extra.md", "# 商品\n\n## 参数\n" + "参数说明。" * 130)
        self.write("docs_new/account_store/account_security_test.md", "# 账号\n\n## 操作\n账号操作。")
        self.faq("faq.csv", [("旧问题", "旧答案", "payment")])
        self.faq("docs_new/account_store/faq.csv", [("账号问题", "账号答案", "account"), ("店铺问题", "店铺答案", "store")])

    def write(self, relative, text):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def faq(self, relative, rows):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("question", "answer", "category"))
            writer.writerows(rows)

    def run_merge(self, **options):
        return merge.merge_content(self.out, self.faq_out, data_dir=self.data, **options)

    def test_full_merge_override_keep_add_faq_and_stats_equal_ingest_dry_run(self):
        before = {path: path.read_bytes() for path in self.data.rglob("*") if path.is_file()}
        with patch("socket.socket", side_effect=AssertionError("禁止联网")):
            stats = self.run_merge()
        self.assertEqual(stats["documents"], 5)
        self.assertEqual(stats["faqs"], 3)
        self.assertEqual(stats["categories"], {"product": 2, "after_sales": 1, "logistics": 1, "account": 1})
        self.assertEqual(stats["faq_categories"], {"payment": 1, "account": 1, "store": 1})
        self.assertEqual(set(stats["overrides"]), {"product_catalog.md", "after_sales_warranty.md"})
        self.assertIn("新目录", (self.out / "product_catalog.md").read_text())
        self.assertIn("保留", (self.out / "logistics_keep.md").read_text())
        actual = ingest_directory(self.out, dry_run=True)
        for key in ("documents", "parent_chunks", "child_chunks"):
            self.assertEqual(stats[key], actual[key])
        with self.faq_out.open(newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["question", "answer", "category"])
        self.assertEqual(len(rows), 4)
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload)

    def test_dry_run_never_writes_but_reports_stats_and_missing_directories(self):
        with patch.object(Path, "write_bytes", side_effect=AssertionError("dry-run 不写文件")), \
                patch.object(Path, "write_text", side_effect=AssertionError("dry-run 不写文件")):
            stats = self.run_merge(dry_run=True)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.faq_out.exists())
        self.assertGreater(stats["child_chunks"], stats["documents"])
        self.assertTrue(stats["messages"])
        self.assertTrue(stats["dry_run"])

    def test_new_conflict_fails_before_writing_and_same_content_is_deduplicated(self):
        self.write("docs_new/guides/product_catalog.md", "# 冲突内容")
        with self.assertRaisesRegex(ValueError, "同名不同内容冲突"):
            self.run_merge()
        self.assertFalse(self.out.exists())
        self.write("docs_new/guides/product_catalog.md", (self.data / "docs_generated/product_catalog.md").read_text())
        self.assertEqual(self.run_merge(dry_run=True)["documents"], 5)

    def test_new_groups_and_new_to_new_conflicts(self):
        self.write("docs_new/extra/store_note.md", "# 店铺信息")
        self.assertEqual(self.run_merge(dry_run=True)["documents"], 6)
        self.write("docs_new/service/store_note.md", "# 不同的店铺信息")
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.run_merge(dry_run=True)

    def test_force_protects_both_targets_and_removes_stale_output(self):
        self.faq_out.write_text("existing FAQ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "--force"):
            self.run_merge()
        self.run_merge(force=True)
        (self.out / "stale.md").write_text("旧文件")
        for dry_run in (False, True):
            with self.assertRaisesRegex(ValueError, "--force"):
                self.run_merge(dry_run=dry_run)
        self.run_merge(force=True)
        self.assertFalse((self.out / "stale.md").exists())

    def test_force_never_allows_other_paths_or_target_symlink(self):
        for out, faq_out in ((self.data / "docs", self.faq_out), (self.out, self.data / "faq.csv"),
                             (self.data.parent / "docs_staging", self.faq_out)):
            with self.assertRaisesRegex(ValueError, "输出仅允许"):
                merge.merge_content(out, faq_out, force=True, data_dir=self.data)
        self.out.symlink_to(self.data / "docs", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "输出仅允许"):
            self.run_merge(force=True)

    def test_source_symlink_and_bad_faq_fail_before_output(self):
        link = self.data / "docs_new/account_store/product_link.md"
        link.symlink_to(self.data / "docs/product_catalog.md")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.run_merge()
        link.unlink()
        self.write("docs_new/account_store/faq.csv", "answer,question,category\n")
        with self.assertRaisesRegex(ValueError, "表头"):
            self.run_merge()
        self.assertFalse(self.out.exists())
        self.assertFalse(self.faq_out.exists())

    def test_cli_status_and_allowed_default_data_targets(self):
        with patch.object(merge, "PROJECT_ROOT", self.data.parent), redirect_stdout(io.StringIO()):
            self.assertEqual(merge.main(["--out", str(self.out), "--faq-out", str(self.faq_out), "--dry-run"]), 0)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(merge.main(["--out", str(self.data / "docs"), "--faq-out", str(self.faq_out)]), 1)

    def test_promoted_inputs_merge_idempotently_and_faq_conflicts_still_fail(self):
        first = self.run_merge()
        for path in self.out.glob("*.md"):
            shutil.copyfile(path, self.data / "docs" / path.name)
        shutil.copyfile(self.faq_out, self.data / "faq.csv")
        second = self.run_merge(force=True)
        for key in ("documents", "parent_chunks", "child_chunks", "faqs", "faq_categories"):
            self.assertEqual(first[key], second[key])
        self.faq("docs_new/account_store/faq.csv", [("账号问题", "不同答案", "account")])
        with self.assertRaisesRegex(ValueError, "FAQ 问题重复或内容冲突"):
            self.run_merge(force=True)

    def test_same_source_duplicate_faq_is_rejected(self):
        self.faq("faq.csv", [("旧问题", "旧答案", "payment")] * 2)
        with self.assertRaisesRegex(ValueError, "FAQ 问题重复或内容冲突"):
            self.run_merge()
        self.assertFalse(self.out.exists())
        self.faq("faq.csv", [("旧问题", "旧答案", "payment")])
        self.faq("docs_new/extra/faq.csv", [("账号问题", "账号答案", "account")])
        with self.assertRaisesRegex(ValueError, "FAQ 问题重复或内容冲突"):
            self.run_merge()


if __name__ == "__main__":
    unittest.main()
