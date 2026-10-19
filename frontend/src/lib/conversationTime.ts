import type { Conversation } from './storage';

export const HISTORY_GROUPS = ['今天', '昨天', '近 7 天', '更早'] as const;

/** Local calendar boundaries, including daylight-saving transitions. */
export function groupConversations(conversations: Conversation[], now: number) {
  const today = new Date(now);
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const week = new Date(today);
  week.setDate(week.getDate() - 6);
  return HISTORY_GROUPS.map((label, index) => ({ label, conversations: [...conversations]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .filter(item => {
      const group = item.updatedAt >= today.getTime() ? 0 : item.updatedAt >= yesterday.getTime() ? 1
        : item.updatedAt >= week.getTime() ? 2 : 3;
      return group === index;
    }) }));
}

export function relativeTime(timestamp: number, now: number): string {
  const minutes = Math.max(0, Math.floor((now - timestamp) / 60_000));
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 1440)} 天前`;
}
