import { describe, expect, it } from 'vitest';
import { conversation } from '../test/conversations';
import { groupConversations, relativeTime } from './conversationTime';

const now = new Date(2026, 9, 18, 12).getTime();
const at = (day: number, hour = 12) => new Date(2026, 9, day, hour).getTime();

describe('conversation time labels', () => {
  it('groups at injected local calendar boundaries and sorts newest first', () => {
    const input = [conversation('older', at(11, 23)), conversation('today', at(18, 0)),
      conversation('yesterday', at(17, 0)), conversation('week-start', at(12, 0)),
      conversation('week-end', at(16, 23)), conversation('newest', now)];
    expect(groupConversations(input, now).map(group => [group.label, group.conversations.map(item => item.id)])).toEqual([
      ['今天', ['newest', 'today']], ['昨天', ['yesterday']], ['近 7 天', ['week-end', 'week-start']], ['更早', ['older']],
    ]);
    expect(input[0].id).toBe('older');
  });
  it('uses calendar dates across month/year boundaries', () => {
    const time = new Date(2026, 0, 1, 0, 1).getTime();
    expect(groupConversations([conversation('last-year', new Date(2025, 11, 31, 23, 59).getTime())], time)[1].conversations).toHaveLength(1);
  });
  it.each([[0, '刚刚'], [30_000, '刚刚'], [60_000, '1 分钟前'], [3_600_000, '1 小时前'], [86_400_000, '1 天前'], [-60_000, '刚刚']])
    ('formats elapsed time %i', (elapsed, label) => { expect(relativeTime(now - Number(elapsed), now)).toBe(label); });
});
