import ReactMarkdown from 'react-markdown';
import type { Source } from '../api/types';
import { remarkCitations } from '../lib/citations';

interface Props { answer: string; sources: Source[]; onCitation: (index: number) => void }

export function AnswerMarkdown({ answer, sources, onCitation }: Props) {
  return <div className="answer-markdown break-words text-[15px] leading-[1.8]">
    <ReactMarkdown skipHtml remarkPlugins={[[remarkCitations, { indices: sources.map(source => source.index) }]]}
      components={{
        sup: ({ node, children }) => {
          const index = Number(node?.properties['data-citation-index'] ?? node?.properties.dataCitationIndex);
          return sources.some(source => source.index === index)
            ? <sup><button type="button" aria-label={`查看引用 ${index}`} className="citation-button" onClick={() => onCitation(index)}>[{index}]</button></sup>
            : <sup>{children}</sup>;
        },
        a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer" className="text-link underline underline-offset-4">{children}</a>,
        // A model-provided image must not make external network requests or track a reader.
        img: ({ alt }) => <span>{alt}</span>,
      }}
    >{answer}</ReactMarkdown>
  </div>;
}
