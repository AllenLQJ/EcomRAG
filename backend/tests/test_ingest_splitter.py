import unittest

from ingest.loaders import Document
from ingest.splitter import split_document, split_parents, split_text


class SplitterTests(unittest.TestCase):
    def document(self, text, source="after_sales_demo.md"):
        return Document("demo123", "测试标题", text, source, "after_sales")

    def test_markdown_heading_levels_and_fences(self):
        doc = self.document("# 标题\n介绍。\n## 条件\n正文。\n### 细则\n内容。\n```text\n## 代码内标题\n```\n## 流程\n提交。")
        parents = split_parents(doc)
        self.assertEqual(len(parents), 2)
        self.assertTrue(parents[0].startswith("# 标题\n介绍。\n## 条件"))
        self.assertIn("### 细则", parents[0])
        self.assertIn("## 代码内标题", parents[0])
        self.assertTrue(parents[1].startswith("## 流程"))

    def test_deep_headings_stay_with_entire_second_level_section(self):
        doc = self.document("# 标题\n简介\n## 条件\n资格\n### 材料\n照片\n#### 角度\n近景\n## 处理\n审核")
        parents = split_parents(doc)
        self.assertEqual(len(parents), 2)
        self.assertIn("#### 角度\n近景", parents[0])
        self.assertNotIn("## 处理", parents[0])
        self.assertEqual(parents[1], "## 处理\n审核")

    def test_non_markdown_paragraphs(self):
        doc = self.document("第一段正文。\n\n第二段正文。\n\n第三段正文。", "demo.txt")
        self.assertEqual(split_parents(doc), ["第一段正文。", "第二段正文。", "第三段正文。"])

    def test_parent_limit_and_lossless_long_section(self):
        doc = self.document("# 标题\n" + "退货条件应结合订单核对。" * 240)
        parents = split_parents(doc)
        self.assertGreater(len(parents), 2)
        self.assertTrue(all(len(parent) <= 1200 for parent in parents))
        self.assertEqual("".join(parents), doc.text.strip())

    def test_child_budget_overlap_title_and_metadata(self):
        doc = self.document("# 标题\n" + "甲乙丙丁戊己庚辛壬癸。" * 60)
        chunks = split_document(doc, child_chunk_size=120, child_chunk_overlap=20)
        prefix = doc.title + "\n"
        self.assertGreater(len(chunks), 2)
        for index, chunk in enumerate(chunks):
            self.assertLessEqual(len(chunk.child_text), 120)
            self.assertTrue(chunk.child_text.startswith(prefix))
            self.assertEqual(chunk.chunk_id, f"demo123#0#{index}")
            self.assertEqual(chunk.parent_id, "demo123#0")
            self.assertEqual((chunk.doc_id, chunk.title, chunk.source, chunk.category),
                             (doc.doc_id, doc.title, doc.source, doc.category))
            if index:
                self.assertEqual(chunks[index - 1].child_text[-20:], chunk.child_text[len(prefix):][:20])
        bodies = [chunk.child_text[len(prefix):] for chunk in chunks]
        self.assertEqual(bodies[0] + "".join(body[20:] for body in bodies[1:]), chunks[0].parent_text)

    def test_chinese_sentence_boundaries(self):
        parts = split_text("退货需要核对订单。照片要清楚。请保留包装！材料是否齐全？" * 12, 50, 5)
        self.assertTrue(all(part[-1] in "。！？；\n" for part in parts[:-1]))
        self.assertTrue(all(len(part) <= 50 for part in parts))

    def test_invalid_sizes(self):
        for size, overlap in [(0, 0), (20, 20), (20, -1)]:
            with self.subTest(size=size, overlap=overlap), self.assertRaises(ValueError):
                split_text("内容", size, overlap)
        with self.assertRaises(ValueError):
            split_document(self.document("正文"), child_chunk_size=10, child_chunk_overlap=5)
