"""仅标准库，验证拆成任意 token 时内部标记不会泄漏。"""
import asyncio
import unittest

from app.llm.answer_stream import AnswerBuffer, forward_answer
from app.llm.generator import clean_generated_answer


class AnswerStreamTests(unittest.TestCase):
    def test_no_answer_marker_is_buffered_for_every_split(self):
        for text in ('NO_ANSWER', ' n o _ a n s w e r ', '**NO_ANSWER**[1]。',
                     '**NO_ANSWER**[1, 2]。', 'NO_ANSWER【１，２】',
                     'ＮＯ＿ＡＮＳＷＥＲ', 'ｎ ｏ ＿ ａ ｎ ｓ ｗ ｅ ｒ'):
            for split in range(len(text) + 1):
                buffer = AnswerBuffer()
                parts = [buffer.feed(text[:split]), buffer.feed(text[split:]), buffer.feed('', final=True)]
                self.assertEqual(''.join(parts), '', (text, split))

    def test_embedded_markers_removed_even_between_individual_characters(self):
        buffer = AnswerBuffer()
        result = ''.join(buffer.feed(char) for char in '已知NO_ANSWER说明 n o _ a n s w e r [1]')
        result += buffer.feed('', final=True)
        self.assertEqual(result, '已知说明  [1]')

    def test_real_answer_is_emitted_as_soon_as_prefix_is_excluded(self):
        buffer = AnswerBuffer()
        self.assertEqual(buffer.feed('N'), '')
        self.assertEqual(buffer.feed('O'), '')
        self.assertEqual(buffer.feed('，你好'), 'NO，你好')
        self.assertEqual(buffer.feed('。'), '。')
        self.assertEqual(buffer.feed(''), '')

    def test_partial_marker_is_discarded_at_eof(self):
        for text in ('NO_ANS', ' n o _ a n s ', 'ＮＯ＿ＡＮＳ', 'NO_ANSWE'):
            buffer = AnswerBuffer()
            self.assertEqual(buffer.feed(text), '')
            self.assertEqual(buffer.feed('', final=True), '')

    def test_partial_marker_after_real_content_does_not_leak(self):
        buffer = AnswerBuffer()
        self.assertEqual(buffer.feed('正常正文ＮＯ＿ＡＮＳ'), '正常正文')
        self.assertEqual(buffer.feed('', final=True), '')

    def test_fullwidth_matching_preserves_the_rest_of_the_answer(self):
        self.assertEqual(clean_generated_answer('型号Ａ，正文。ＮＯ＿ＡＮＳＷＥＲ'), ('型号Ａ，正文。', False))
        self.assertEqual(clean_generated_answer('ＮＯ＿ＡＮＳＷＥＲ'), ('', True))
        buffer = AnswerBuffer()
        text = '型号Ａ，ＮＯ＿ＡＮＳＷＥＲ正文。'
        visible = ''.join(buffer.feed(char) for char in text) + buffer.feed('', final=True)
        self.assertEqual(visible, '型号Ａ，正文。')

    def test_forwarding_returns_refusal_input_for_only_a_partial_marker(self):
        async def run(text):
            async def source():
                for char in text:
                    yield char
            visible = []
            async def emit(part):
                visible.append(part)
            answer = await forward_answer(source(), emit)
            return answer, visible
        answer, visible = asyncio.run(run('ＮＯ＿ＡＮＳ'))
        self.assertEqual((answer, visible), ('', []))
        self.assertEqual(clean_generated_answer(answer), ('', True))
        answer, visible = asyncio.run(run('已知说明NO_ANS'))
        self.assertEqual(answer, '已知说明')
        self.assertEqual(''.join(visible), answer)

    def test_forwarding_closes_source_on_consumer_error(self):
        async def run():
            closed = []
            async def source():
                try:
                    yield '正常'
                    yield '文本'
                finally:
                    closed.append(True)
            async def emit(text):
                raise RuntimeError('consumer stopped')
            with self.assertRaises(RuntimeError):
                await forward_answer(source(), emit)
            self.assertEqual(closed, [True])
        asyncio.run(run())
