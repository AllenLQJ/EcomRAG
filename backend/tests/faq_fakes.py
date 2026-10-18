"""FAQ 测试替身：不依赖 jieba、rank_bm25、Redis 或 MySQL。"""
import math
from collections import Counter


class FakeRepo:
    def __init__(self, records):
        self.records = records
        self.loads = 0

    def load_enabled(self):
        self.loads += 1
        return list(self.records)


class FakeBM25:
    """独立的小型词频替身，用于验证分词、ID 映射、排序和 reload。"""

    def __init__(self, corpus):
        self.rows = [Counter(tokens) for tokens in corpus]

    def get_scores(self, tokens):
        frequencies = Counter(token for row in self.rows for token in row)
        return [sum(row[token] * math.log(1 + len(self.rows) / frequencies[token])
                    for token in tokens if token in row) for row in self.rows]


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, *, ex):
        self.values[key] = value
        self.writes.append((key, value, ex))

    def close(self):
        pass
