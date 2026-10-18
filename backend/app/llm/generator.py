import re
from dataclasses import dataclass

from app.llm.prompts import block, load


@dataclass(frozen=True)
class GeneratedAnswer:
    answer: str
    cited_indices: set[int]


def validate_citations(answer, count):
    cited = set()

    def replace(match):
        # 避免不可信输出中的超长整数字符串触发 int 限制。
        digits = match.group(1).lstrip("0") or "0"
        if len(digits) > len(str(count)):
            return ""
        index = int(digits)
        if 1 <= index <= count:
            cited.add(index)
            return f"[{index}]"
        return ""

    return GeneratedAnswer(re.sub(r"\[([0-9]+)\]", replace, answer), cited)


class Generator:
    def __init__(self, client, model, *, max_tokens=600):
        self.client, self.model, self.max_tokens = client, model, max_tokens

    async def generate_answer(self, question, docs, history):
        materials = "\n\n".join(f"[{i}] {doc.title}\n{doc.content}" for i, doc in enumerate(docs, 1))
        answer = await self.client.complete(
            model=self.model, temperature=0.2, max_tokens=self.max_tokens,
            messages=[{"role": "system", "content": load("generate")},
                      {"role": "user", "content": block("资料", materials) + "\n" + block("问题", question)}],
        )
        return validate_citations(answer, len(docs))

    async def chat_general(self, question, history):
        return await self.client.complete(
            model=self.model, temperature=0.2, max_tokens=self.max_tokens,
            messages=[{"role": "system", "content": load("general")},
                      {"role": "user", "content": block("问题", question)}],
        )
