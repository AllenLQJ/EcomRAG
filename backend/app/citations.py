"""用生成器实际读取的正文核对引用；仅作词面校验，不推断语义蕴含。"""
import re
import unicodedata
from dataclasses import dataclass

from app.llm.generator import CODE, CITATION, GeneratedAnswer, validate_citations


TOKEN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*|[\u3400-\u9fff]+")
MODEL = re.compile(r"(?<![a-z0-9])[a-z]+[-_]?\d+[a-z0-9_-]*(?![a-z0-9])")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
END = re.compile(r"[。！？!?；;\n]|\.(?!\d)")
GROUP = re.compile(r"\[\d+\](?:[ \t]*\[\d+\])*")
LIST_PREFIX = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)")


@dataclass(frozen=True)
class Features:
    words: set[str]
    models: set[str]
    numbers: set[str]


def features(text):
    text = unicodedata.normalize("NFKC", LIST_PREFIX.sub("", CITATION.sub("", text))).lower()
    models = set(MODEL.findall(text))
    # 型号中的数字不作为规格数字：L1 不能为「功率 1 瓦」提供数字证据。
    numbers = set(NUMBER.findall(MODEL.sub(" ", text)))
    words = set()
    for token in TOKEN.findall(text):
        if "\u3400" <= token[0] <= "\u9fff":
            words.update(token[i:i + 2] for i in range(len(token) - 1))
        elif not any(char.isdigit() for char in token):
            words.add(token)
    return Features(words, models, numbers)


def overlap(claim, evidence, *, strict=False):
    words = claim.words & evidence.words
    models = claim.models & evidence.models
    numbers = claim.numbers & evidence.numbers
    if strict:
        # 只有同类商品名、相邻型号或不同规格数字相似，不能当作事实支持。
        if (claim.models and not models) or (claim.numbers and not numbers):
            return 0.0
        if not words and (claim.words or not (models or numbers)):
            return 0.0
        if words and len(words) < 2 and not (models or numbers or len(claim.words) == 1):
            return 0.0
    shared = len(words) + 3 * len(models) + 2 * len(numbers)
    total = len(claim.words) + 3 * len(claim.models) + 2 * len(claim.numbers)
    evidence_size = len(evidence.words) + 3 * len(evidence.models) + 2 * len(evidence.numbers)
    # 覆盖回答为主，少量精度权重使短的事实句优先于夹杂大量无关文字的窗口。
    return shared / max(1, total) + 0.05 * shared / max(1, evidence_size)


def sentences(text):
    """保留偏移；句末引用（包括「事实。[1][2]」）归属于前一句。"""
    start = 0
    for match in END.finditer(text):
        if match.start() < start:
            continue
        end = match.end()
        tail = re.match(r"(?:[ \t]*\[\d+\])*", text[end:])
        end += tail.end()
        yield start, end
        start = end
    if start < len(text):
        yield start, len(text)


def windows(text, limit=120):
    for start, end in sentences(text):
        sentence = text[start:end].strip()
        if not sentence:
            continue
        if len(sentence) <= limit:
            yield sentence
        else:
            # 长段落也检查尾部，避免重新退化成固定截取开头。
            for offset in range(0, len(sentence) - limit, limit // 2):
                yield sentence[offset:offset + limit]
            yield sentence[-limit:]


def align_citations(answer, contents):
    generated = validate_citations(answer, len(contents))
    answer = generated.answer
    # 代码中的编号保持原样，也不能成为正文引用的事实依据。
    masked = CODE.sub(lambda match: " " * len(match.group()), answer)
    evidence = []
    for content in contents:
        identity = features(content).models
        passages = []
        for window in windows(content):
            item = features(window)
            # 单商品父块常在标题写型号、下一行写规格；继承唯一型号，避免误删。
            if len(identity) == 1 and not item.models:
                item = Features(item.words, identity, item.numbers)
            passages.append(item)
        evidence.append(passages)
    claims, removals = {}, []
    for start, end in sentences(masked):
        sentence = masked[start:end]
        previous, last_claim = 0, ""
        for group in GROUP.finditer(sentence):
            claim = sentence[previous:group.start()]
            if not any(char.isalnum() for char in claim):
                claim = last_claim or CITATION.sub("", sentence)
            last_claim, previous = claim, group.end()
            query = features(claim)
            for marker in re.finditer(r"\[(\d+)\]", group.group()):
                index = int(marker[1])
                score = max((overlap(query, item, strict=True) for item in evidence[index - 1]), default=0)
                if score >= 0.24:
                    claims.setdefault(index, []).append(claim)
                else:
                    offset = start + group.start()
                    removals.append((offset + marker.start(), offset + marker.end()))
    for start, end in reversed(removals):
        answer = answer[:start] + answer[end:]
    return GeneratedAnswer(answer, set(claims)), claims


def best_source(answer, contents):
    query = features(CODE.sub("", answer))
    # 并列时按上下文原顺序；检索分数不参与回退排序。
    return max(range(len(contents)), key=lambda i: overlap(query, features(contents[i]))) + 1


def source_snippet(content, answer, question, limit=120):
    query, question_query = features(CODE.sub("", answer)), features(question)
    return max(windows(content, limit),
               key=lambda window: (overlap(query, features(window)),
                                   overlap(question_query, features(window))), default="")
