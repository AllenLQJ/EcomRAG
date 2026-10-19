import { describe, expect, it } from 'vitest';
import { turn } from '../test/conversations';
import { buildHistory } from './history';

describe('request history', () => {
  it('takes three completed successful turns in chronological, alternating order', () => {
    const messages = ['一', '二', '三', '四'].flatMap(question => turn(question));
    expect(buildHistory(messages)).toEqual(['二', '三', '四'].flatMap(question => [
      { role: 'user', content: question }, { role: 'assistant', content: `回答：${question}` },
    ]));
  });
  it('strips citations before truncating both roles to 500 Unicode code points', () => {
    expect(buildHistory(turn('😀'.repeat(501), '[1]' + '字[22]'.repeat(501)))).toEqual([
      { role: 'user', content: '😀'.repeat(500) }, { role: 'assistant', content: '字'.repeat(500) },
    ]);
  });
  it('excludes errors with their questions, orphan replies and unfinished questions', () => {
    expect(buildHistory([...turn('错误', '', true), ...turn('完整'),
      { id: 'orphan', kind: 'user', text: '未完成' }, ...turn('下一个'),
      { id: 'pending', kind: 'user', text: '正在生成' }])).toEqual(buildHistory([...turn('完整'), ...turn('下一个')]));
    expect(buildHistory([turn('孤立')[1]])).toEqual([]);
    expect(buildHistory([])).toEqual([]);
  });
  it('preserves refusal wording as assistant context', () => {
    const messages = turn('火星', '抱歉，店铺资料里没有找到相关说明。建议联系人工客服确认。');
    const reply = messages[1];
    if (reply.kind === 'assistant') reply.response.route = 'fallback';
    expect(buildHistory(messages)[1].content).toBe('抱歉，店铺资料里没有找到相关说明。建议联系人工客服确认。');
  });
});
