"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { SourceView } from "@/types/api";

type Props = {
  text: string;
  sources?: SourceView[];
  /** Called with a source's index when its citation is clicked in the text. */
  onCite?: (index: number) => void;
};

const CITE = "#cite-";

/** "[POL-RETURNS-001 v1 §3. How to refund]" -> "POL-RETURNS-001 §3" */
export function shortCitation(citation: string): string {
  const inner = citation.replace(/^\[|\]$/g, "");
  const match = inner.match(/^(\S+)(?:\s+v\d+)?\s+§\s*([\w.]+?)\.?(?:\s|$)/);
  return match ? `${match[1]} §${match[2]}` : inner;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Turns each cited source in the reply into a link the page can catch. */
function linkCitations(text: string, sources: SourceView[]): string {
  let result = text;
  sources.forEach((source, index) => {
    if (!source.citation) return;
    const label = source.citation.replace(/^\[|\]$/g, "").replace(/[[\]]/g, "");
    result = result.replace(new RegExp(escapeRegExp(source.citation), "g"), `[${label}](${CITE}${index})`);
  });
  return result;
}

/**
 * The assistant's reply, rendered from Markdown. Raw HTML in the text is never
 * rendered (react-markdown's default), and links open in a new tab.
 */
export function Markdown({ text, sources = [], onCite }: Props) {
  return (
    <div className="prose-reply">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ href, children }) {
            if (href?.startsWith(CITE)) {
              const index = Number(href.slice(CITE.length));
              const source = sources[index];
              return (
                <button
                  type="button"
                  onClick={() => onCite?.(index)}
                  title={source ? `${source.title}, ${source.section}` : undefined}
                  className="mx-0.5 inline-flex translate-y-[-1px] items-center rounded border border-rule-strong bg-sheet-2 px-1.5 text-[0.8em] font-semibold text-ink-2 hover:border-khata hover:text-khata"
                >
                  {source ? shortCitation(source.citation) : children}
                </button>
              );
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer" className="text-focus underline underline-offset-2">
                {children}
              </a>
            );
          },
        }}
      >
        {linkCitations(text, sources)}
      </ReactMarkdown>
    </div>
  );
}
