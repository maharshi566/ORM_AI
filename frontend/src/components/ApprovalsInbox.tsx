"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ApprovalSlip } from "@/components/ApprovalSlip";
import { RequireLogin } from "@/components/RequireLogin";
import { Notice, PageTitle, RecordId, Spinner, StatusBadge } from "@/components/ui";
import { errorMessage, getApproval, listApprovals } from "@/lib/api";
import { ago, toolLabel, when } from "@/lib/format";
import type { Login } from "@/lib/login";
import { canDecide, type Decision } from "@/lib/turns";
import type { ApprovalListItem, ApprovalRequestView, ChatResponse } from "@/types/api";

type Tab = "pending" | "decided";

export function ApprovalsInbox() {
  return <RequireLogin>{(login) => <Inbox login={login} />}</RequireLogin>;
}

/** Pending approvals grouped by request: one slip per workflow. */
function byWorkflow(items: ApprovalListItem[]): ApprovalListItem[][] {
  const groups = new Map<string, ApprovalListItem[]>();
  for (const item of items) groups.set(item.workflow_id, [...(groups.get(item.workflow_id) ?? []), item]);
  return [...groups.values()];
}

function Inbox({ login }: { login: Login }) {
  const [tab, setTab] = useState<Tab>("pending");
  const [items, setItems] = useState<ApprovalListItem[] | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [done, setDone] = useState<{ workflowId: string; reply: ChatResponse; decision: Decision } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listApprovals({ limit: 100 }, controller.signal).then(
      (result) => {
        setItems(result.approvals);
        setCounts(result.counts);
        setError(null);
      },
      (err: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      },
    );
    return () => controller.abort();
  }, [refresh]);

  const pending = useMemo(() => byWorkflow((items ?? []).filter((i) => i.status === "pending")), [items]);
  const decided = useMemo(() => (items ?? []).filter((i) => i.status !== "pending"), [items]);
  const decidedCount = (counts.approved ?? 0) + (counts.modified ?? 0) + (counts.rejected ?? 0);

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6">
      <PageTitle title="Approvals">
        What ORM_AI wants to do and is waiting for a person to decide. Nothing on this list has happened yet.
      </PageTitle>

      <div role="group" aria-label="Which approvals" className="mt-6 flex gap-1 border-b border-rule">
        {(
          [
            ["pending", `Waiting (${counts.pending ?? 0})`],
            ["decided", `Decided (${decidedCount})`],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-pressed={tab === id}
            onClick={() => setTab(id)}
            className={`-mb-px border-b-2 px-4 py-2 font-medium ${
              tab === id ? "border-khata text-ink" : "border-transparent text-ink-2 hover:text-ink"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {error ? (
        <div className="mt-6">
          <Notice title="Could not load the approvals">{error}</Notice>
        </div>
      ) : null}
      {items === null && !error ? (
        <div className="mt-6">
          <Spinner label="Loading the approvals" />
        </div>
      ) : null}

      {done ? (
        <div className="mt-6">
          <Notice
            tone={outcome(done.reply, done.decision).tone}
            title={outcome(done.reply, done.decision).title}
            action={
              <span className="flex flex-wrap gap-4">
                {done.reply.session_id ? (
                  <Link href={`/chat?session=${encodeURIComponent(done.reply.session_id)}`} className="font-medium underline underline-offset-2">
                    Open the conversation
                  </Link>
                ) : null}
                <Link href={`/workflows/${done.workflowId}`} className="font-medium underline underline-offset-2">
                  Open the full record
                </Link>
              </span>
            }
          >
            {done.reply.proposed_actions
              .filter((a) => a.result)
              .map((a) => (
                <span key={a.action_id ?? a.tool} className="block">
                  {toolLabel(a.tool)}: {a.result}
                </span>
              ))}
          </Notice>
        </div>
      ) : null}

      {items !== null && tab === "pending" ? (
        pending.length === 0 ? (
          <p className="mt-8 text-ink-2">
            Nothing is waiting. When a request in{" "}
            <Link href="/chat" className="font-medium text-ink underline underline-offset-2">
              Chat
            </Link>{" "}
            needs a decision, it appears here too.
          </p>
        ) : (
          <ul className="mt-6 space-y-4">
            {pending.map((group) => (
              <PendingRequest
                key={group[0].workflow_id}
                group={group}
                login={login}
                open={open === group[0].workflow_id}
                onToggle={() => setOpen((o) => (o === group[0].workflow_id ? null : group[0].workflow_id))}
                onDecided={(reply, decision) => {
                  setDone({ workflowId: group[0].workflow_id, reply, decision });
                  setOpen(null);
                  setRefresh((n) => n + 1);
                }}
              />
            ))}
          </ul>
        )
      ) : null}

      {items !== null && tab === "decided" ? (
        decided.length === 0 ? (
          <p className="mt-8 text-ink-2">No decisions yet.</p>
        ) : (
          <div className="mt-6 overflow-x-auto rounded-lg border border-rule bg-sheet">
            <table className="w-full min-w-[44rem] text-left text-sm">
              <thead className="border-b border-rule bg-sheet-2 text-ink-2">
                <tr>
                  <th className="px-4 py-2 font-medium">Decided</th>
                  <th className="px-4 py-2 font-medium">Action</th>
                  <th className="px-4 py-2 font-medium">Decision</th>
                  <th className="px-4 py-2 font-medium">By</th>
                  <th className="px-4 py-2 font-medium">Note</th>
                  <th className="px-4 py-2 font-medium">
                    <span className="sr-only">Record</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-rule">
                {decided.map((item) => (
                  <tr key={item.approval_id}>
                    <td className="whitespace-nowrap px-4 py-2 text-ink-2">{when(item.decided_at)}</td>
                    <td className="px-4 py-2">
                      <span className="font-medium">{toolLabel(item.tool)}</span>
                      <span className="block text-ink-3">{argumentSummary(item)}</span>
                    </td>
                    <td className="px-4 py-2">
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="px-4 py-2">{item.decided_by === login.userId ? "You" : item.decided_by}</td>
                    <td className="px-4 py-2 text-ink-2">{item.decision_note ?? ""}</td>
                    <td className="px-4 py-2 text-right">
                      <Link href={`/workflows/${item.workflow_id}`} className="text-focus underline underline-offset-2">
                        Record
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : null}
    </div>
  );
}

/** What to say once a decision is saved: only what the tools confirmed. */
function outcome(reply: ChatResponse, decision: Decision): { tone: "done" | "info" | "problem"; title: string } {
  const failed = reply.proposed_actions.filter((a) => a.status === "failed").length;
  const done = reply.proposed_actions.filter((a) => a.status === "done").length;
  if (decision.status === "rejected") return { tone: "info", title: "Rejected. Nothing was changed." };
  if (failed) return { tone: "problem", title: `Decision saved, but ${failed} action(s) could not be carried out.` };
  if (done) return { tone: "done", title: "Decision saved, and the approved work was carried out." };
  return { tone: "info", title: "Decision saved." };
}

/** The records an action touches, e.g. "SALE-005598" or "PRD-0002". */
function argumentSummary(item: ApprovalListItem): string {
  return Object.entries(item.arguments)
    .filter(([key]) => key.endsWith("_id"))
    .map(([, value]) => String(value))
    .join(", ");
}

function PendingRequest({
  group,
  login,
  open,
  onToggle,
  onDecided,
}: {
  group: ApprovalListItem[];
  login: Login;
  open: boolean;
  onToggle: () => void;
  onDecided: (reply: ChatResponse, decision: Decision) => void;
}) {
  const first = group[0];
  const required = group.some((i) => i.required_role === "owner") ? "owner" : "staff";
  const allowed = canDecide(login.role, required);
  const [request, setRequest] = useState<ApprovalRequestView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    getApproval(first.workflow_id, controller.signal).then(
      (status) => {
        if (status.approval) setRequest(status.approval);
        else setError("This request is no longer waiting. Someone may have decided it already.");
      },
      (err: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      },
    );
    return () => controller.abort();
  }, [open, first.workflow_id]);

  return (
    <li className="rounded-lg border border-rule bg-sheet">
      <div className="flex flex-wrap items-start justify-between gap-4 px-5 py-4">
        <div className="min-w-0 flex-1">
          <p className="font-semibold">
            {group.map((i) => toolLabel(i.tool)).join(", ")}
            {group.map((i) => argumentSummary(i)).filter(Boolean).length ? (
              <span className="ml-2 font-normal text-ink-2">
                <RecordId>{group.map((i) => argumentSummary(i)).join("; ")}</RecordId>
              </span>
            ) : null}
          </p>
          {first.user_query ? <p className="mt-1 max-w-[65ch] text-ink-2">“{first.user_query}”</p> : null}
          <p className="mt-1 text-sm text-ink-3">
            Asked {ago(first.created_at)}
            {login.role === "admin" ? ` in ${first.shop_id}` : ""}. {required === "owner" ? "The owner decides." : "Staff or the owner can decide."}
            {first.estimate ? ` ${first.estimate}.` : ""}
          </p>
        </div>
        <button
          type="button"
          aria-expanded={open}
          onClick={onToggle}
          className={`rounded-md px-4 py-2 font-semibold ${
            open ? "border border-rule text-ink-2" : allowed ? "bg-ink text-sheet hover:opacity-90" : "border border-ink text-ink"
          }`}
        >
          {open ? "Close" : allowed ? "Review and decide" : "Review"}
        </button>
      </div>
      {open ? (
        <div className="border-t border-rule px-5 pb-5">
          {error ? (
            <div className="mt-4">
              <Notice>{error}</Notice>
            </div>
          ) : request ? (
            <ApprovalSlip request={request} login={login} onDecided={onDecided} />
          ) : (
            <div className="mt-4">
              <Spinner label="Loading the evidence" />
            </div>
          )}
        </div>
      ) : null}
    </li>
  );
}
