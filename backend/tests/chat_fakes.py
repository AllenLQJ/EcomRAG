"""整个聊天服务替身，不创建 SDK、网络客户端或模型。"""
from unittest.mock import AsyncMock, Mock

from app.contracts import RetrievedDoc, RouteDecision
from app.llm.generator import GeneratedAnswer


def document(index=1, score=0.8):
    return RetrievedDoc(parent_id=f"p{index}", doc_id=f"d{index}", title=f"资料{index}",
                        source=f"doc{index}.md", content="父块完整内容。说明。", snippet="子块摘要", score=score)


class FakeServices:
    def __init__(self):
        self.faq_lookup = Mock(return_value=None)
        self.route = AsyncMock(return_value=RouteDecision(intent="general", rewritten_query="店铺服务"))
        self.chat_general = AsyncMock(return_value="你好，我是灵码客服助手。")
        self.retrieve = Mock(return_value=[document()])
        self.generate_answer = AsyncMock(return_value=GeneratedAnswer("说明[1]", {1}))
        self.warmup = AsyncMock()

    async def chat_general_stream(self, question, history):
        yield await self.chat_general(question, history)

    async def generate_answer_stream(self, question, docs, history):
        yield (await self.generate_answer(question, docs, history)).answer

    def knowledge(self):
        self.route.return_value = RouteDecision(intent="knowledge", rewritten_query="改写后的检索问题")
        return self

    def retrieve_candidates(self, queries):
        return self.retrieve(queries, rerank_query=queries[0], top_k=5)

    def rerank_candidates(self, rerank_query, candidates, top_k=5):
        return candidates[:top_k]
