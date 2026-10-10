"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApprovalSlip } from "@/components/ApprovalSlip";
import { SourceList, StageRail } from "@/components/chat/RequestPanel";
import { Markdown } from "@/components/Markdown";
import { RequireLogin } from "@/components/RequireLogin";
import { Notice, PageTitle, SectionTitle, Spinner, StatusBadge } from "@/components/ui";
import { errorMessage, getWorkflow } from "@/lib/api";
import { agentLabel, count, ms, showValue, toolLabel, when } from "@/lib/format";
import type { Login } from "@/lib/login";
import { railFromReply } from "@/lib/turns";
import type { WorkflowResponse } from "@/types/api";

export function WorkflowRecord({ workflowId }: { workflowId: string }) {
  return <RequireLogin>{(login) => <Record workflowId={workflowId} login={login} />}</RequireLogin>;
}

function Record({ workflowId, login }: { workflowId: string; login: Login }) {
  const [workflow, setWorkflow] = useState<WorkflowResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [highlight, setHighlight] = useState<{ index: number; tick: number } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getWorkflow(workflowId, controller.signal).then(
      (result) => setWorkflow(result),
      (err: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      },
    );
    return () => controller.abort();
  }, [workflowId, refresh]);

  if (error) {
    return (
      <div className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6">
        <Notice title="Could not open this record">{error}</Notice>
      </div>
    );
  }
  if (!workflow) {
    return (
      <div className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6">
        <Spinner label="Loading the record" />
      </div>
    );
  }

  const reply = workflow.reply;
  const sources = reply?.sources ?? [];
  const tokens = workflow.agents.reduce((total, a) => total + (a.input_tokens ?? 0) + (a.output_tokens ?? 0), 0);
  const cite = (index: number) => setHighlight((h) => ({ index, tick: (h?.tick ?? 0) + 1 }));

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6">
      <p className="text-sm text-ink-2">
        <Link href="/admin" className="underline underline-offset-2">
          Admin
        </Link>{" "}
        / Request record
      </p>
      <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
        <PageTitle title={workflow.user_query} />
        <StatusBadge status={workflow.status} />
      </div>
      <p className="mt-2 text-sm text-ink-2">
        {workflow.shop_id}, asked {when(workflow.created_at)}
        {workflow.intent ? `, about ${workflow.intent.replace(/_/g, " ")}` : ""}. {workflow.agents.length} agent steps,{" "}
        {workflow.tool_calls.length} tool calls, {count(tokens)} tokens.
        {workflow.session_id ? (
          <>
            {" "}
            <Link href={`/chat?session=${encodeURIComponent(workflow.session_id)}`} className="font-medium text-focus underline underline-offset-2">
              Open the conversation
            </Link>
          </>
        ) : null}
      </p>

      <div className="mt-8 grid gap-10 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-10">
          <section>
            <SectionTitle title="The answer" />
            {workflow.approval ? (
              <ApprovalSlip
                request={workflow.approval}
                login={login}
                sources={sources}
                onCite={cite}
                onDecided={() => setRefresh((n) => n + 1)}
              />
            ) : workflow.final_response ? (
              <div className="max-w-[70ch] rounded-lg border border-rule bg-sheet p-5">
                <Markdown text={workflow.final_response} sources={sources} onCite={cite} />
              </div>
            ) : (
              <p className="text-ink-2">{workflow.error ?? "No answer was recorded."}</p>
            )}
          </section>

          <section>
            <SectionTitle title="Agent steps" />
            <ol className="overflow-hidden rounded-lg border border-rule bg-sheet">
              {workflow.agents.map((step, i) => (
                <li key={i} className="grid grid-cols-[7rem_minmax(0,1fr)_auto] gap-x-4 border-b border-rule px-4 py-2.5 text-sm last:border-b-0">
                  <span className="font-semibold">{agentLabel(step.agent)}</span>
                  <span className="min-w-0 break-words text-ink-2">
                    {step.summary}
                    {step.error ? <span className="block text-amber">{step.error}</span> : null}
                    {step.model ? <span className="block text-xs text-ink-3">{step.model}</span> : null}
                  </span>
                  <span className="text-right text-xs text-ink-3 tabular-nums">
                    {ms(step.latency_ms)}
                    {step.input_tokens || step.output_tokens ? (
                      <span className="block">
                        {count(step.input_tokens)} in, {count(step.output_tokens)} out
                      </span>
                    ) : null}
                  </span>
                </li>
              ))}
            </ol>
          </section>

          <section>
            <SectionTitle title="Tool calls" />
            {workflow.tool_calls.length === 0 ? (
              <p className="text-ink-2">No tools were called.</p>
            ) : (
              <ol className="space-y-2">
                {workflow.tool_calls.map((call, i) => (
                  <li key={i} className="rounded-lg border border-rule bg-sheet px-4 py-2.5 text-sm">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span>
                        <span className="font-semibold">{toolLabel(call.tool)}</span>
                        <span className="text-ink-3"> by {agentLabel(call.agent)}</span>
                      </span>
                      <span className="flex items-center gap-2 text-xs text-ink-3">
                        {call.status !== "success" ? <StatusBadge status="error" label={call.error_code ?? "failed"} /> : null}
                        {ms(call.latency_ms)}
                      </span>
                    </div>
                    <p className="mt-1 break-words text-ink-2">
                      {Object.entries(call.arguments)
                        .map(([k, v]) => `${k}: ${showValue(v)}`)
                        .join(", ") || "no arguments"}
                    </p>
                  </li>
                ))}
              </ol>
            )}
          </section>

          {workflow.approvals.length ? (
            <section>
              <SectionTitle title="Approvals" />
              <ul className="divide-y divide-rule rounded-lg border border-rule bg-sheet text-sm">
                {workflow.approvals.map((a) => (
                  <li key={a.approval_id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-2.5">
                    <span className="font-medium">{toolLabel(a.tool)}</span>
                    <span className="text-ink-2">
                      {a.decided_by ? `${a.decided_by}, ${when(a.decided_at)}` : `needs ${a.required_role ?? "a person"}`}
                      {a.decision_note ? `: ${a.decision_note}` : ""}
                    </span>
                    <StatusBadge status={a.status} />
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>

        <aside className="space-y-8">
          {reply ? (
            <section>
              <SectionTitle title="Stages" />
              <StageRail rail={railFromReply(reply)} live={false} />
            </section>
          ) : null}
          <section>
            <SectionTitle title="Sources" />
            {reply ? (
              <SourceList sources={sources} highlight={highlight?.index ?? null} tick={highlight?.tick ?? 0} />
            ) : (
              <p className="text-sm text-ink-2">The saved state of this request is gone, so its sources cannot be shown.</p>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
