/** Plain-text display only; keep the API source and stored conversation intact. */
export function cleanSourceSnippet(title: string, snippet: string): string {
  const normalizeTitle = (value: string) => value.replace(/\s+/g, '').replace(/｜/g, '|');
  const lines = snippet.split(/\r?\n/).map(line => {
    let text = line.trim();
    // Nested blockquotes/lists can carry more than one leading marker.
    const marker = /^(?:#{1,6}(?:\s+|$)|>\s*|[-*+](?:\s+|$)|\d+[.)]\s+)/;
    while (marker.test(text)) text = text.replace(marker, '').trim();
    return text.replace(/(\*\*|__)(.*?)\1/g, '$2').trim();
  }).filter(line => line && !/^(?:[-*_]\s*){3,}$/.test(line));

  const heading = normalizeTitle(title);
  while (heading && lines.length && normalizeTitle(lines[0]).startsWith(heading)) lines.shift();
  const text = lines.join(' ').replace(/\s+/g, ' ')
    .replace(/(?:\.{3,}|…+)(?:\s*(?:\.{3,}|…+))*/g, '…').trim();
  return text && !/^[.…\s]+$/.test(text) ? text : '暂无摘要，请结合回答中的引用查看相关说明。';
}
