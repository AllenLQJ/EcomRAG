"""FAQ 测试替身：不依赖 jieba、rank_bm25、Redis 或 MySQL。"""
import math
from collections import Counter
from fnmatch import fnmatchcase


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

    def delete(self, *keys):
        count = sum(key in self.values for key in keys)
        for key in keys:
            self.values.pop(key, None)
        return count

    def eval(self, script, numkeys, key, expected):
        assert numkeys == 1 and "GET" in script and "DEL" in script
        return self.delete(key) if self.values.get(key) == expected else 0

    def scan(self, cursor=0, match=None, count=200):
        return 0, [key for key in self.values if match is None or fnmatchcase(key, match)]

    def close(self):
        pass
