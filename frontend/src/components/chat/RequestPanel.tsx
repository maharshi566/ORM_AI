"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";

import { shortCitation } from "@/components/Markdown";
import { agentLabel, count, ms, STAGES, toolLabel } from "@/lib/format";
import type { Rail, StageState } from "@/lib/turns";
import type { ChatResponse, SourceView, ToolActivity } from "@/types/api";

const DOT: Record<StageState["state"], string> = {
  waiting: "border-rule-strong bg-sheet",
  active: "working border-khata bg-khata",
  paused: "border-khata bg-khata-soft",
  done: "border-stamp bg-stamp",
  failed: "border-amber bg-amber",
  skipped: "border-rule bg-sheet-2",
};

const STATE_TEXT: Record<StageState["state"], string> = {
  waiting: "Not yet",
  active: "Working",
  paused: "Waiting for a person",
  done: "Done",
  failed: "Had a problem",
  skipped: "Not needed",
};

/** The workflow panel: which stage the request is in, and what each stage did. */
export function StageRail({ rail, live }: { rail: Rail; live: boolean }) {
  return (
    <div>
      <ol className="relative" aria-label="Workflow stages">
        {STAGES.map((stage, index) => {
          const s = rail.stages[stage.id];
          const last = index === STAGES.length - 1;
          const muted = s.state === "skipped" || s.state === "waiting";
          return (
            <li key={stage.id} className="relative flex gap-3 pb-3" aria-current={s.state === "active" || s.state === "paused" ? "step" : undefined}>
              {!last ? <span aria-hidden className="absolute left-[5px] top-4 h-[calc(100%-0.5rem)] w-px bg-rule" /> : null}
              <span aria-hidden className={`relative mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full border-2 ${DOT[s.state]}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className={`text-sm ${muted ? "text-ink-3" : "font-semibold"} ${s.state === "skipped" ? "line-through decoration-rule-strong" : ""}`}>
                    {stage.label}
                  </span>
                  <span className={`shrink-0 text-xs ${s.state === "paused" || s.state === "active" ? "font-semibold text-khata" : "text-ink-3"}`}>
                    {s.state === "done" && s.latencyMs ? ms(s.latencyMs) : STATE_TEXT[s.state]}
                  </span>
                </div>
                {s.summary && (s.state === "done" || s.state === "failed") ? (
                  <p className="mt-0.5 text-xs leading-snug text-ink-2 break-words">{s.summary}</p>
                ) : null}
                {s.state === "active" && s.active.length ? (
                  <p className="mt-0.5 text-xs text-ink-2">{s.active.map(agentLabel).join(" and ")} agent working…</p>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
      {rail.supervisor ? (
        <p className="mt-1 rounded-md bg-sheet-2 px-3 py-2 text-xs leading-snug text-ink-2">
          <span className="font-semibold text-ink">Supervisor:</span> {rail.supervisor}
        </p>
      ) : null}
      {live ? <p className="sr-only" aria-live="polite">{STAGES.find((st) => rail.stages[st.id].state === "active")?.label ?? ""}</p> : null}
    </div>
  );
}

export function ToolList({ calls }: { calls: ToolActivity[] }) {
  if (!calls.length) return <p className="text-sm text-ink-3">No tools were needed.</p>;
  return (
    <ul className="divide-y divide-rule text-sm">
      {calls.map((call, i) => (
        <li key={`${call.tool}-${i}`} className="flex items-baseline justify-between gap-3 py-1.5">
          <span className="min-w-0">
            <span className="font-medium">{toolLabel(call.tool)}</span>
            <span className="text-ink-3"> by {agentLabel(call.agent)}</span>
            {call.status !== "success" ? (
              <span className="block text-xs text-amber">Failed{call.error_code ? `: ${call.error_code.replace(/_/g, " ")}` : ""}</span>
            ) : null}
          </span>
          <span className="shrink-0 text-xs text-ink-3 tabular-nums">{ms(call.latency_ms)}</span>
        </li>
      ))}
    </ul>
  );
}

export function SourceList({
  sources,
  highlight = null,
  tick = 0,
}: {
  sources: SourceView[];
  highlight?: number | null;
  tick?: number;
}) {
  const refs = useRef<(HTMLLIElement | null)[]>([]);
  useEffect(() => {
    if (highlight === null) return;
    refs.current[highlight]?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [highlight, tick]);

  if (!sources.length) return <p className="text-sm text-ink-3">This answer did not need the shop&apos;s rules.</p>;
  return (
    <ol className="space-y-3">
      {sources.map((source, i) => (
        <li
          key={`${source.citation}-${i}`}
          id={`source-${i}`}
          ref={(el) => {
            refs.current[i] = el;
          }}
          className={`rounded-md border p-3 text-sm transition-colors ${highlight === i ? "border-khata bg-khata-soft/50" : "border-rule bg-sheet"}`}
        >
          <p className="font-semibold leading-snug">{source.title}</p>
          <p className="text-ink-2">
            {source.section} <span className="text-ink-3">{shortCitation(source.citation)}</span>
          </p>
          <blockquote className="mt-2 border-l-2 border-rule-strong pl-3 text-ink-2 whitespace-pre-line">{source.excerpt}</blockquote>
          {source.trust !== "trusted" ? (
            <p className="mt-2 text-xs font-semibold text-amber">An uploaded reference, not a shop rule: used for facts only.</p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function Section({ title, aside, children }: { title: string; aside?: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-rule px-4 py-4 first:border-t-0">
      <h2 className="mb-2.5 flex items-baseline justify-between font-serif text-[15px] font-bold">
        {title}
        {aside ? <span className="font-sans text-xs font-normal text-ink-3">{aside}</span> : null}
      </h2>
      {children}
    </section>
  );
}

/** The right-hand panel: the selected request's stages, sources and tools. */
export function RequestPanel({
  rail,
  live,
  reply,
  highlight,
  tick,
}: {
  rail: Rail | null;
  live: boolean;
  reply: ChatResponse | null;
  highlight: number | null;
  tick: number;
}) {
  if (!rail) {
    return (
      <div className="px-4 py-6 text-sm text-ink-2">
        Ask something to see the agents at work: which records they read, which rules they used, and what waits for you.
      </div>
    );
  }
  return (
    <div>
      <Section title="How this request went" aside={reply ? ms(reply.usage.latency_ms) : live ? "working…" : undefined}>
        <StageRail rail={rail} live={live} />
      </Section>
      {reply ? (
        <>
          <Section title="Sources" aside={reply.sources.length ? `${reply.sources.length} cited` : undefined}>
            <SourceList sources={reply.sources} highlight={highlight} tick={tick} />
          </Section>
          <Section title="Tools used" aside={reply.tool_calls.length ? `${reply.tool_calls.length} calls` : undefined}>
            <ToolList calls={reply.tool_calls} />
          </Section>
          <Section title="Cost of this answer">
            <p className="text-sm text-ink-2">
              {count(reply.usage.llm_calls)} model calls, {count(reply.usage.input_tokens + reply.usage.output_tokens)} tokens
              {reply.validation ? `; reply check: ${reply.validation === "PASS" ? "passed" : reply.validation.toLowerCase()}` : ""}.
            </p>
            <Link href={`/workflows/${reply.workflow_id}`} className="mt-2 inline-block text-sm font-medium text-focus underline underline-offset-2">
              Open the full record of this request
            </Link>
          </Section>
        </>
      ) : null}
    </div>
  );
}
