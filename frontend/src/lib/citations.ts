export type CitationPart = { type: 'text'; text: string } | { type: 'citation'; index: number };

/** Only called on Markdown text nodes, never on inlineCode/code nodes. */
export function parseCitations(text: string, indices: ReadonlySet<number>): CitationPart[] {
  const parts: CitationPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(/\[([1-9]\d*)\]/g)) {
    const index = Number(match[1]);
    if (!indices.has(index)) continue;
    const start = match.index!;
    if (start > cursor) parts.push({ type: 'text', text: text.slice(cursor, start) });
    parts.push({ type: 'citation', index });
    cursor = start + match[0].length;
  }
  if (cursor < text.length) parts.push({ type: 'text', text: text.slice(cursor) });
  return parts;
}

// A structural subset of mdast avoids adding a runtime dependency just for traversal.
interface MarkdownNode {
  type: string;
  value?: string;
  children?: MarkdownNode[];
  data?: { hName?: string; hProperties?: Record<string, unknown> };
}

export function remarkCitations(options: { indices: number[] }) {
  const indices = new Set(options.indices);
  return (tree: MarkdownNode): void => {
    const visit = (node: MarkdownNode): void => {
      if (['code', 'inlineCode', 'html', 'link', 'linkReference'].includes(node.type) || !node.children) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== 'text' || child.value === undefined) {
          visit(child);
          return [child];
        }
        return parseCitations(child.value, indices).map<MarkdownNode>(part => part.type === 'text'
          ? { type: 'text', value: part.text }
          : {
              type: 'citation',
              children: [{ type: 'text', value: `[${part.index}]` }],
              data: { hName: 'sup', hProperties: { 'data-citation-index': part.index } },
            });
      });
    };
    visit(tree);
  };
}
