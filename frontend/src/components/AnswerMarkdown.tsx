import ReactMarkdown from 'react-markdown';
import type { Source } from '../api/types';
import { useMobile } from '../hooks/useMobile';
import { remarkCitations } from '../lib/citations';

interface Props { answer: string; sources: Source[]; onCitation: (index: number) => void }

function allowedLink(href: string | undefined): boolean {
  if (!href) return false;
  try { return ['http:', 'https:', 'mailto:'].includes(new URL(href).protocol); }
  catch { return false; }
}

export function AnswerMarkdown({ answer, sources, onCitation }: Props) {
  const mobile = useMobile();
  return <div>
    {/* The system font stack includes static fonts; weight 500 applies only to answer prose. */}
    <div className="answer-markdown break-words text-base font-medium leading-[1.8]">
      <ReactMarkdown skipHtml remarkPlugins={[[remarkCitations, { indices: sources.map(source => source.index) }]]}
        components={{
          sup: ({ node, children }) => {
            const index = Number(node?.properties['data-citation-index'] ?? node?.properties.dataCitationIndex);
            if (!sources.some(source => source.index === index)) return <sup>{children}</sup>;
            if (mobile) return <sup aria-hidden="true">[{index}]</sup>;
            return <sup><button type="button" aria-label={`查看引用 ${index}`} className="citation-button" onClick={() => onCitation(index)}>[{index}]</button></sup>;
          },
          a: ({ href, children }) => allowedLink(href)
            ? <a href={href} target="_blank" rel="noopener noreferrer" className="text-link underline underline-offset-4">{children}</a>
            : <>{children}</>,
          // A model-provided image must not make external network requests or track a reader.
          img: ({ alt }) => <span>{alt}</span>,
        }}
      >{answer}</ReactMarkdown>
    </div>
    {mobile && sources.length > 0 && <div role="group" aria-label="回答引用" className="mt-2 flex flex-wrap items-center gap-1 text-xs text-muted">
      <span>引用</span>
      {sources.map(source => <button key={source.index} type="button" aria-label={`查看引用 ${source.index}`}
        className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md font-mono text-xs text-accent hover:bg-line"
        onClick={() => onCitation(source.index)}>[{source.index}]</button>)}
    </div>}
  </div>;
}
