"use client";

import Link from "next/link";
import { Fragment, useEffect, useState } from "react";

import { RequireLogin } from "@/components/RequireLogin";
import { Notice, PageTitle, SectionTitle, Spinner, StatusBadge } from "@/components/ui";
import { ApiError, errorMessage, getEvaluations, getMetrics, listWorkflows } from "@/lib/api";
import { agentLabel, count, ms, percent, toolLabel, when } from "@/lib/format";
import type { Login } from "@/lib/login";
import type { EvaluationRunView, MetricsResponse, WorkflowListItem } from "@/types/api";

const GATE = 0.8;
const SCORE_LABEL: Record<string, string> = {
  intent: "Intent understood",
  records: "Right records found",
  cited: "Right rules cited",
  grounded: "Reply passed the check",
  approval: "Paused for the right person",
  no_false_claims: "No false claims of action",
};
const SCORE_SHORT: Record<string, string> = {
  intent: "Intent",
  records: "Records",
  cited: "Rules",
  grounded: "Checked",
  approval: "Approval",
  no_false_claims: "Honest",
};
const STATUSES = ["", "completed", "awaiting_approval", "failed", "blocked", "running"] as const;
// Agents in the order a request meets them.
const AGENT_ORDER = ["triage", "supervisor", "data_retrieval", "knowledge", "investigation", "human_review", "action", "respond", "clarify", "validate", "finalize"];

type Load<T> = { kind: "loading" } | { kind: "ready"; value: T } | { kind: "error"; message: string; status?: number };

function useLoad<T>(fetcher: (signal: AbortSignal) => Promise<T>, deps: unknown[]): Load<T> {
  const [state, setState] = useState<Load<T>>({ kind: "loading" });
  useEffect(() => {
    const controller = new AbortController();
    fetcher(controller.signal).then(
      (value) => setState({ kind: "ready", value }),
      (err: unknown) => {
        if (controller.signal.aborted) return;
        setState({ kind: "error", message: errorMessage(err), status: err instanceof ApiError ? err.status : undefined });
      },
    );
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the caller lists what the fetch depends on
  }, deps);
  return state;
}

export function AdminDashboard() {
  return <RequireLogin>{(login) => <Dashboard login={login} />}</RequireLogin>;
}

function Dashboard({ login }: { login: Login }) {
  const [status, setStatus] = useState<string>("");
  const metrics = useLoad((signal) => getMetrics(signal), []);
  const workflows = useLoad((signal) => listWorkflows({ status: status || undefined, limit: 25 }, signal), [status]);
  const evaluations = useLoad((signal) => getEvaluations(signal), []);

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6">
      <PageTitle title="Admin">
        How the agents are doing: every request, each agent&apos;s time and errors, the tools they called and the
        evaluation scores.
        {metrics.kind === "ready" ? ` Covers ${metrics.value.scope === "all shops" ? "every shop" : login.shopName ?? metrics.value.scope}.` : ""}
      </PageTitle>

      {metrics.kind === "loading" ? (
        <div className="mt-8">
          <Spinner label="Counting" />
        </div>
      ) : null}
      {metrics.kind === "error" ? (
        <div className="mt-8">
          <Notice title="Could not load the figures">{metrics.message}</Notice>
        </div>
      ) : null}
      {metrics.kind === "ready" ? <Overview metrics={metrics.value} /> : null}

      <section className="mt-12">
        <SectionTitle title="Recent requests">
          <label className="flex items-center gap-2">
            Show
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              className="rounded-md border border-rule bg-sheet px-2 py-1 text-ink"
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s ? s.replace(/_/g, " ") : "all"}
                </option>
              ))}
            </select>
          </label>
        </SectionTitle>
        {workflows.kind === "error" ? <Notice>{workflows.message}</Notice> : null}
        {workflows.kind === "loading" ? <Spinner label="Loading requests" /> : null}
        {workflows.kind === "ready" ? <WorkflowTable rows={workflows.value.workflows} showShop={login.role === "admin"} /> : null}
      </section>

      {metrics.kind === "ready" ? (
        <>
          <section className="mt-12">
            <SectionTitle title="Agents">Time per run; the bar is the slowest 5% (p95)</SectionTitle>
            <AgentTable metrics={metrics.value} />
          </section>
          <section className="mt-12">
            <SectionTitle title="Tools" />
            <ToolTable metrics={metrics.value} />
          </section>
        </>
      ) : null}

      <section className="mt-12">
        <SectionTitle title="Evaluation" />
        {evaluations.kind === "loading" ? <Spinner label="Loading the evaluation runs" /> : null}
        {evaluations.kind === "error" ? (
          <Notice tone="info">
            {evaluations.status === 403
              ? "Evaluation scores cover every shop, so only an admin sees them. Log in as the ORM_AI Admin (USR-101)."
              : evaluations.message}
          </Notice>
        ) : null}
        {evaluations.kind === "ready" ? (
          evaluations.value.runs.length ? (
            <Evaluation runs={evaluations.value.runs} />
          ) : (
            <Notice tone="info">
              No evaluation has been saved yet. From the backend folder, run <code>python -m scripts.eval_agent</code>; its
              scores appear here.
            </Notice>
          )
        ) : null}
      </section>
    </div>
  );
}

function Overview({ metrics }: { metrics: MetricsResponse }) {
  const w = metrics.workflows;
  const a = metrics.approvals;
  const errors = metrics.agents.reduce((n, x) => n + x.errors, 0) + metrics.tools.reduce((n, x) => n + x.errors, 0);
  const rows: [string, string, string][] = [
    ["Requests", count(w.total), `${count(metrics.workflows_last_24h)} in the last 24 hours`],
    ["Answered", count(w.completed), ""],
    ["Waiting for approval", count(w.awaiting_approval), a.pending ? `${count(a.pending)} action(s) to decide` : ""],
    ["Failed or stopped", count((w.failed ?? 0) + (w.blocked ?? 0)), `${count(errors)} agent and tool errors`],
    ["Decisions", count((a.approved ?? 0) + (a.modified ?? 0) + (a.rejected ?? 0)), `${count(a.approved)} approved, ${count(a.modified)} changed, ${count(a.rejected)} rejected`],
    ["Knowledge base", count(metrics.documents.documents), `${count(metrics.documents.chunks)} searchable passages`],
  ];
  return (
    <dl className="mt-8 grid grid-cols-2 border-y border-rule sm:grid-cols-3 lg:grid-cols-6">
      {rows.map(([label, value, note]) => (
        <div key={label} className="border-rule px-1 py-4 odd:border-r sm:border-r sm:px-4 sm:[&:nth-child(3n)]:border-r-0 lg:[&:nth-child(3n)]:border-r lg:last:border-r-0">
          <dt className="text-sm text-ink-2">{label}</dt>
          <dd className="mt-0.5 font-serif text-2xl font-extrabold tabular-nums">{value}</dd>
          {note ? <dd className="text-xs leading-snug text-ink-3">{note}</dd> : null}
        </div>
      ))}
    </dl>
  );
}

function WorkflowTable({ rows, showShop }: { rows: WorkflowListItem[]; showShop: boolean }) {
  if (!rows.length) return <p className="text-ink-2">No requests yet.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-rule bg-sheet">
      <table className="w-full min-w-[44rem] text-left text-sm">
        <thead className="border-b border-rule bg-sheet-2 text-ink-2">
          <tr>
            <th className="px-4 py-2 font-medium">Asked</th>
            {showShop ? <th className="px-4 py-2 font-medium">Shop</th> : null}
            <th className="px-4 py-2 font-medium">Request</th>
            <th className="px-4 py-2 font-medium">About</th>
            <th className="px-4 py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-rule">
          {rows.map((row) => (
            <tr key={row.workflow_id} className="hover:bg-sheet-2">
              <td className="whitespace-nowrap px-4 py-2 text-ink-2">{when(row.created_at)}</td>
              {showShop ? <td className="whitespace-nowrap px-4 py-2 text-ink-2">{row.shop_id}</td> : null}
              <td className="px-4 py-2">
                <Link href={`/workflows/${row.workflow_id}`} className="line-clamp-2 font-medium hover:underline">
                  {row.user_query}
                </Link>
              </td>
              <td className="px-4 py-2 text-ink-2">{row.intent?.replace(/_/g, " ") ?? ""}</td>
              <td className="px-4 py-2">
                <StatusBadge status={row.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AgentTable({ metrics }: { metrics: MetricsResponse }) {
  if (!metrics.agents.length) return <p className="text-ink-2">No agent has run yet.</p>;
  const slowest = Math.max(...metrics.agents.map((a) => a.p95_latency_ms), 1);
  const rank = (agent: string) => (AGENT_ORDER.indexOf(agent) + AGENT_ORDER.length + 1) % (AGENT_ORDER.length + 1);
  const agents = [...metrics.agents].sort((a, b) => rank(a.agent) - rank(b.agent));
  return (
    <div className="overflow-x-auto rounded-lg border border-rule bg-sheet">
      <table className="w-full min-w-[44rem] text-left text-sm">
        <thead className="border-b border-rule bg-sheet-2 text-ink-2">
          <tr>
            <th className="px-4 py-2 font-medium">Agent</th>
            <th className="px-4 py-2 text-right font-medium">Runs</th>
            <th className="px-4 py-2 text-right font-medium">Errors</th>
            <th className="px-4 py-2 text-right font-medium">Average</th>
            <th className="w-[30%] px-4 py-2 font-medium">Slowest 5%</th>
            <th className="px-4 py-2 text-right font-medium">Tokens in / out</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-rule tabular-nums">
          {agents.map((a) => (
            <tr key={a.agent}>
              <td className="px-4 py-2 font-medium">{agentLabel(a.agent)}</td>
              <td className="px-4 py-2 text-right">{count(a.runs)}</td>
              <td className={`px-4 py-2 text-right ${a.errors ? "font-semibold text-amber" : "text-ink-3"}`}>{count(a.errors)}</td>
              <td className="px-4 py-2 text-right">{ms(a.avg_latency_ms)}</td>
              <td className="px-4 py-2">
                <div className="flex items-center gap-3" title={`${agentLabel(a.agent)}: 95% of runs took ${ms(a.p95_latency_ms)} or less`}>
                  <div className="h-2 flex-1 rounded-r bg-sheet-2">
                    <div className="h-2 rounded-r-[4px] bg-ink-2" style={{ width: `${Math.max(2, (a.p95_latency_ms / slowest) * 100)}%` }} />
                  </div>
                  <span className="w-14 text-right">{ms(a.p95_latency_ms)}</span>
                </div>
              </td>
              <td className="px-4 py-2 text-right text-ink-2">
                {count(a.input_tokens)} / {count(a.output_tokens)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ToolTable({ metrics }: { metrics: MetricsResponse }) {
  if (!metrics.tools.length) return <p className="text-ink-2">No tool has been called yet.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-rule bg-sheet">
      <table className="w-full min-w-[40rem] text-left text-sm">
        <thead className="border-b border-rule bg-sheet-2 text-ink-2">
          <tr>
            <th className="px-4 py-2 font-medium">Tool</th>
            <th className="px-4 py-2 text-right font-medium">Calls</th>
            <th className="px-4 py-2 text-right font-medium">Failed</th>
            <th className="px-4 py-2 font-medium">Why they failed</th>
            <th className="px-4 py-2 text-right font-medium">Average time</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-rule tabular-nums">
          {metrics.tools.map((t) => (
            <tr key={t.tool}>
              <td className="px-4 py-2">
                <span className="font-medium">{toolLabel(t.tool)}</span> <span className="text-ink-3">{t.tool}</span>
              </td>
              <td className="px-4 py-2 text-right">{count(t.calls)}</td>
              <td className={`px-4 py-2 text-right ${t.errors ? "font-semibold text-amber" : "text-ink-3"}`}>{count(t.errors)}</td>
              <td className="px-4 py-2 text-ink-2">
                {Object.entries(t.error_codes)
                  .map(([code, n]) => `${code.replace(/_/g, " ")} (${n})`)
                  .join(", ")}
              </td>
              <td className="px-4 py-2 text-right">{ms(t.avg_latency_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Evaluation({ runs }: { runs: EvaluationRunView[] }) {
  const [index, setIndex] = useState(0);
  const run = runs[index];
  const names = Object.keys(SCORE_LABEL).filter((name) => name in run.scores);
  const passes = names.every((name) => run.scores[name] >= GATE);
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-ink-2">
          {when(run.created_at)}, {run.cases} cases, {run.passed} passed every check
          {run.models ? <span className="block text-sm text-ink-3">Models: {run.models}</span> : null}
        </p>
        {runs.length > 1 ? (
          <label className="flex items-center gap-2 text-sm text-ink-2">
            Run
            <select value={index} onChange={(e) => setIndex(Number(e.target.value))} className="rounded-md border border-rule bg-sheet px-2 py-1 text-ink">
              {runs.map((r, i) => (
                <option key={r.run_id} value={i}>
                  {when(r.created_at)} ({r.passed}/{r.cases})
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      <div className="mt-4 rounded-lg border border-rule bg-sheet p-5">
        <p className="font-semibold">
          {passes ? "Passes the gate" : "Below the gate"}: every score must reach {percent(GATE)}.
        </p>
        <ul className="mt-4 grid gap-x-8 gap-y-3 sm:grid-cols-2">
          {names.map((name) => {
            const value = run.scores[name];
            const ok = value >= GATE;
            return (
              <li key={name}>
                <div className="flex items-baseline justify-between text-sm">
                  <span>{SCORE_LABEL[name]}</span>
                  <span className={`font-semibold tabular-nums ${ok ? "text-stamp" : "text-amber"}`}>
                    {percent(value)} {ok ? "" : "(below gate)"}
                  </span>
                </div>
                <div className="relative mt-1 h-2 rounded bg-sheet-2" title={`${SCORE_LABEL[name]}: ${percent(value)}`}>
                  <div className={`h-2 rounded-r-[4px] ${ok ? "bg-stamp" : "bg-amber"}`} style={{ width: `${Math.max(1, value * 100)}%` }} />
                  <div aria-hidden className="absolute -top-1 h-4 w-0.5 bg-ink" style={{ left: `${GATE * 100}%` }} />
                </div>
              </li>
            );
          })}
        </ul>
        <p className="mt-3 text-xs text-ink-3">The dark tick on each bar is the {percent(GATE)} gate.</p>
      </div>

      <div className="mt-4 overflow-x-auto rounded-lg border border-rule bg-sheet">
        <table className="w-full min-w-[48rem] text-left text-sm">
          <thead className="border-b border-rule bg-sheet-2 text-ink-2">
            <tr>
              <th className="px-4 py-2 font-medium">Case</th>
              <th className="px-4 py-2 font-medium">Message</th>
              {names.map((name) => (
                <th key={name} className="px-2 py-2 text-center font-medium" title={SCORE_LABEL[name]}>
                  {SCORE_SHORT[name] ?? name}
                </th>
              ))}
              <th className="px-4 py-2 text-right font-medium">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-rule">
            {run.results.map((c) => (
              <Fragment key={c.case_id}>
                <tr className={c.passed ? "" : "bg-amber-soft/40"}>
                  <td className="whitespace-nowrap px-4 py-2 font-medium">
                    {c.case_id}
                    <span className="block text-xs font-normal text-ink-3">{c.category}</span>
                  </td>
                  <td className="px-4 py-2 text-ink-2">
                    <span className="line-clamp-2">{c.details.message}</span>
                  </td>
                  {names.map((name) => (
                    <td key={name} className="px-2 py-2 text-center">
                      {name in c.scores ? (
                        c.scores[name] >= 1 ? (
                          <span className="text-stamp" aria-label="pass">
                            ✓
                          </span>
                        ) : (
                          <span className="font-bold text-amber" aria-label="miss">
                            ✗
                          </span>
                        )
                      ) : (
                        <span className="text-ink-3" aria-label="not scored">
                          –
                        </span>
                      )}
                    </td>
                  ))}
                  <td className="px-4 py-2 text-right text-ink-2 tabular-nums">{ms(c.latency_ms)}</td>
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
