"""增量清洗内部 NO_ANSWER 标记，跨 token 保留潜在前缀。"""
import re

from app.llm.generator import CITATION, normalize_marker_text, strip_marker_prefix, strip_no_answer

MARKER = "NO_ANSWER"


class AnswerBuffer:
    def __init__(self):
        self.pending = ""
        self.opening = ""
        self.started = False

    def feed(self, text, *, final=False):
        self.pending, _ = strip_no_answer(self.pending + text)
        if final:
            self.pending, _ = strip_marker_prefix(self.pending)
        cut = len(self.pending)
        if not final:
            # 只保留可能跨增量边界的标记前缀；允许大小写及字母间空白。
            normalized = normalize_marker_text(self.pending)
            for index, char in enumerate(normalized):
                if char.upper() == "N":
                    suffix = re.sub(r"\s", "", normalized[index:]).upper()
                    if suffix and MARKER.startswith(suffix):
                        cut = index
                        break
        ready, self.pending = self.pending[:cut], self.pending[cut:]
        if self.started:
            return ready
        self.opening += ready
        meaningful = CITATION.sub("", self.opening)
        # 尚未闭合的引用不能以其中数字提前排除 NO_ANSWER（例如标记后分两次到达 [1、]）。
        if not final:
            meaningful = re.sub(r"[\[【［][0-9０-９ ,，、\t]*$", "", meaningful)
        if any(char.isalnum() for char in meaningful):
            self.started = True
            ready, self.opening = self.opening, ""
            return ready
        return ""


async def forward_answer(stream, emit):
    raw = []
    buffer = AnswerBuffer()
    try:
        async for text in stream:
            raw.append(text)
            visible = buffer.feed(text)
            if visible:
                await emit(visible)
    finally:
        await stream.aclose()
    visible = buffer.feed("", final=True)
    if visible:
        await emit(visible)
    if not buffer.started:
        return ""
    return strip_marker_prefix("".join(raw))[0]
