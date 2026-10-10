"use client";

import { useState } from "react";

import { shortCitation } from "@/components/Markdown";
import { Notice, RecordId, StatusBadge } from "@/components/ui";
import { decideApproval, errorMessage } from "@/lib/api";
import { argumentLabel, day, percent, showValue, slipNumber, toolLabel } from "@/lib/format";
import type { Login } from "@/lib/login";
import { canDecide, type Decision } from "@/lib/turns";
import type {
  ActionDecision,
  ApprovalActionView,
  ApprovalDecisionRequest,
  ApprovalRequestView,
  ChatResponse,
  ProposedActionView,
  SourceView,
} from "@/types/api";

type Choice = "approve" | "reject" | "modify";

// Values a field may take, where the tool accepts only a few (backend/app/tools).
const CHOICES: Record<string, string[]> = {
  movement_type: ["adjustment", "damage"],
  issue: ["late", "short"],
  channel: ["whatsapp", "sms"],
  category: ["stock_discrepancy", "supplier_issue", "credit_dispute", "pricing", "customer_complaint", "reorder", "returns"],
};
const LONG_TEXT = new Set(["reason", "description", "notes", "resolution"]);
const INTEGER = new Set(["quantity_change", "quantity"]);
// Money arrives as a decimal string ("125.0") so no rupee is lost to rounding.
const MONEY = new Set(["new_selling_price", "amount", "unit_cost"]);

function rupees(value: unknown): string {
  const number = Number(value);
  return Number.isFinite(number) ? `Rs ${number.toLocaleString("en-IN", { maximumFractionDigits: 2 })}` : showValue(value);
}

type Line = { product_id: string; quantity: number };

function isLines(value: unknown): value is Line[] {
  return Array.isArray(value) && value.every((v) => v && typeof v === "object" && "product_id" in v && "quantity" in v);
}

/** Form text for each argument; ids and product lines stay as they are. */
function draftOf(action: ApprovalActionView): Record<string, string> {
  const draft: Record<string, string> = {};
  for (const [key, value] of Object.entries(action.arguments)) {
    if (isLines(value)) value.forEach((line, i) => (draft[`lines.${i}`] = String(line.quantity)));
    else if (typeof value === "boolean") draft[key] = value ? "true" : "false";
    else draft[key] = value === null || value === undefined ? "" : String(value);
  }
  return draft;
}

/** The arguments to send for "modify", or an error message for the person. */
function argumentsFrom(action: ApprovalActionView, draft: Record<string, string>): Record<string, unknown> | string {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(action.arguments)) {
    if (key.endsWith("_id")) {
      result[key] = value; // a change may not switch to other records
    } else if (isLines(value)) {
      const lines: Line[] = [];
      for (let i = 0; i < value.length; i++) {
        const quantity = Number(draft[`lines.${i}`]);
        if (!Number.isInteger(quantity) || quantity < 1) return `Line ${i + 1}: the quantity must be a whole number of at least 1.`;
        lines.push({ ...value[i], quantity });
      }
      result[key] = lines;
    } else if (typeof value === "boolean") {
      result[key] = draft[key] === "true";
    } else if (typeof value === "number") {
      const number = Number(draft[key]);
      if (draft[key].trim() === "" || !Number.isFinite(number)) return `${argumentLabel(key)} must be a number.`;
      if (INTEGER.has(key) && !Number.isInteger(number)) return `${argumentLabel(key)} must be a whole number.`;
      result[key] = number;
    } else if (MONEY.has(key)) {
      const number = Number(draft[key]);
      if (draft[key].trim() === "" || !Number.isFinite(number) || number <= 0) return `${argumentLabel(key)} must be an amount above 0.`;
      result[key] = typeof value === "number" ? number : draft[key].trim();
    } else if (value === null && draft[key].trim() === "") {
      result[key] = null;
    } else {
      result[key] = draft[key];
    }
  }
  return result;
}

const STAMP: Record<string, { text: string; className: string }> = {
  approved: { text: "Approved", className: "border-stamp text-stamp" },
  modified: { text: "Approved with changes", className: "border-stamp text-stamp" },
  rejected: { text: "Rejected", className: "border-khata text-khata" },
  mixed: { text: "Decided", className: "border-ink-2 text-ink-2" },
};

function Stamp({ decision, who }: { decision: Decision; who: string }) {
  const stamp = STAMP[decision.status] ?? STAMP.mixed;
  return (
    <div
      aria-label={`${stamp.text} by ${who}`}
      className={`stamp pointer-events-none relative mt-3 inline-block rounded-md sm:absolute sm:right-5 sm:top-4 sm:mt-0 border-[3px] border-double px-3 py-1.5 text-center font-serif ${stamp.className}`}
    >
      <div className="text-lg font-extrabold uppercase leading-none tracking-wider">{stamp.text}</div>
      <div className="mt-1 text-[11px] font-bold leading-none">
        {who}
        {decision.at ? `, ${day(decision.at)}` : ""}
      </div>
    </div>
  );
}

type Props = {
  request: ApprovalRequestView;
  login: Login;
  sources?: SourceView[];
  onCite?: (index: number) => void;
  /** Set once a person has decided: the slip is stamped and shows the results. */
  decision?: Decision | null;
  results?: ProposedActionView[];
  onDecided?: (reply: ChatResponse, decision: Decision) => void;
};

/**
 * The approval slip: what ORM_AI wants to do, why, the evidence and rules behind it,
 * and the person's Approve / Modify / Reject. Nothing changes until someone decides.
 */
export function ApprovalSlip({ request, login, sources = [], onCite, decision, results = [], onDecided }: Props) {
  const [choices, setChoices] = useState<Record<string, Choice>>(() =>
    Object.fromEntries(request.actions.map((a) => [a.approval_id, "approve" as Choice])),
  );
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>(() =>
    Object.fromEntries(request.actions.map((a) => [a.approval_id, draftOf(a)])),
  );
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<Choice | "send" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const allowed = canDecide(login.role, request.required_role);
  const single = request.actions.length === 1 ? request.actions[0] : null;
  const decided = Boolean(decision);
  const who = decision?.by === login.userId ? (login.name ?? login.userId) : (decision?.by ?? "");

  async function send(override?: Choice) {
    setError(null);
    const picked: Record<string, Choice> = override
      ? Object.fromEntries(request.actions.map((a) => [a.approval_id, override]))
      : choices;
    const decisions: ActionDecision[] = [];
    for (const action of request.actions) {
      const choice = picked[action.approval_id];
      if (choice === "modify") {
        const args = argumentsFrom(action, drafts[action.approval_id]);
        if (typeof args === "string") {
          setError(args);
          return;
        }
        decisions.push({ approval_id: action.approval_id, decision: "modify", arguments: args });
      } else {
        decisions.push({ approval_id: action.approval_id, decision: choice });
      }
    }
    const kinds = new Set(decisions.map((d) => d.decision));
    const body: ApprovalDecisionRequest =
      kinds.size === 1 && !kinds.has("modify")
        ? { decision: decisions[0].decision as "approve" | "reject" }
        : { decisions };
    if (note.trim()) body.note = note.trim();
    setBusy(override ?? "send");
    try {
      const reply = await decideApproval(request.workflow_id, body);
      const status =
        kinds.size > 1 ? "mixed" : kinds.has("modify") ? "modified" : kinds.has("reject") ? "rejected" : "approved";
      window.dispatchEvent(new Event("orm-ai:approvals-changed"));
      onDecided?.(reply, { status, by: login.userId, note: note.trim() || null, at: new Date().toISOString() });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const resultFor = (action: ApprovalActionView) =>
    results.find((r) => r.approval_id === action.approval_id || r.action_id === action.action_id);

  return (
    <section
      aria-label="Approval slip"
      className="perforated relative mt-4 overflow-hidden rounded-b-lg border border-t-0 border-rule-strong bg-sheet shadow-[0_1px_0_var(--rule)]"
    >
      <div className="border-t-2 border-dashed border-rule-strong px-5 pb-5 pt-5">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 pr-2">
          <h3 className="font-serif text-base font-bold">
            {decided ? "Decided" : "Needs your decision"}
            <span className="ml-2 font-sans text-sm font-normal text-ink-3">
              Slip {slipNumber(request.actions[0]?.approval_id ?? request.workflow_id)}
            </span>
          </h3>
          {!decided ? (
            <p className="text-sm text-ink-2">
              {request.required_role === "owner" ? "The owner decides" : "Staff or the owner can decide"}
              {request.confidence !== null ? `, confidence ${percent(request.confidence)}` : ""}
            </p>
          ) : null}
        </div>

        {decision ? <Stamp decision={decision} who={who} /> : null}

        {request.summary ? <p className={`mt-3 max-w-[62ch] ${decided ? "sm:pr-44" : ""}`}>{request.summary}</p> : null}

        <ol className="mt-4 space-y-4">
          {request.actions.map((action, index) => {
            const choice = choices[action.approval_id];
            const result = resultFor(action);
            return (
              <li key={action.approval_id} className="rounded-md border border-rule bg-sheet-2 p-4">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="font-semibold">
                    {request.actions.length > 1 ? `${index + 1}. ` : ""}
                    {toolLabel(action.tool)}
                  </p>
                  {result ? <StatusBadge status={result.status} /> : null}
                </div>
                <p className="mt-1 text-sm text-ink-2">{action.reason}</p>
                {action.approval_reasons.length ? (
                  <p className="mt-1 text-sm text-ink-2">Needs approval for: {action.approval_reasons.join("; ")}</p>
                ) : null}
                {action.estimate ? <p className="mt-0.5 text-sm text-ink-2">Estimate: {action.estimate}</p> : null}

                {choice === "modify" && !decided ? (
                  <ArgumentEditor
                    action={action}
                    draft={drafts[action.approval_id]}
                    onChange={(draft) => setDrafts((all) => ({ ...all, [action.approval_id]: draft }))}
                  />
                ) : (
                  <dl className="mt-3 grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-4 gap-y-1 text-sm">
                    {Object.entries(action.arguments).map(([key, value]) => (
                      <div key={key} className="contents">
                        <dt className="text-ink-3">{argumentLabel(key)}</dt>
                        <dd className="min-w-0 break-words">
                          {isLines(value) ? (
                            value.map((line) => (
                              <span key={line.product_id} className="mr-3 inline-block">
                                <RecordId>{line.product_id}</RecordId> × {line.quantity}
                              </span>
                            ))
                          ) : key.endsWith("_id") ? (
                            <RecordId>{showValue(value)}</RecordId>
                          ) : MONEY.has(key) ? (
                            rupees(value)
                          ) : (
                            showValue(value)
                          )}
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}

                {result?.result ? <p className="mt-3 border-l-2 border-stamp pl-3 text-sm">{result.result}</p> : null}

                {!decided && allowed && request.actions.length > 1 ? (
                  <fieldset className="mt-3 flex flex-wrap gap-2 text-sm">
                    <legend className="sr-only">Decision for {toolLabel(action.tool)}</legend>
                    {(["approve", "modify", "reject"] as const).map((option) => (
                      <label
                        key={option}
                        className={`cursor-pointer rounded-md border px-3 py-1 ${
                          choice === option ? "border-ink bg-sheet font-semibold" : "border-rule text-ink-2"
                        }`}
                      >
                        <input
                          type="radio"
                          className="sr-only"
                          name={`choice-${action.approval_id}`}
                          checked={choice === option}
                          onChange={() => setChoices((all) => ({ ...all, [action.approval_id]: option }))}
                        />
                        {option === "approve" ? "Approve" : option === "modify" ? "Change, then approve" : "Reject"}
                      </label>
                    ))}
                  </fieldset>
                ) : null}
              </li>
            );
          })}
        </ol>

        {request.findings.length || request.evidence.length ? (
          <details className="mt-4 text-sm">
            <summary className="cursor-pointer font-medium text-ink-2">
              Evidence ({request.findings.length + request.evidence.length})
            </summary>
            <ul className="mt-2 space-y-1.5 pl-1">
              {request.findings.map((finding) => (
                <li key={finding} className="border-l-2 border-rule pl-3">
                  {finding}
                </li>
              ))}
              {request.evidence.map((item, i) => (
                <li key={i} className="border-l-2 border-rule pl-3">
                  {item.fact ?? showValue(item)}
                  {item.reference ? <span className="block text-ink-3">{item.reference}</span> : null}
                </li>
              ))}
            </ul>
          </details>
        ) : null}

        {request.policy_references.length ? (
          <p className="mt-3 flex flex-wrap items-center gap-1.5 text-sm text-ink-2">
            Rules:
            {request.policy_references.map((ref) => {
              const index = sources.findIndex((s) => s.citation === ref);
              return index >= 0 && onCite ? (
                <button
                  key={ref}
                  type="button"
                  onClick={() => onCite(index)}
                  className="rounded border border-rule-strong bg-sheet-2 px-1.5 text-xs font-semibold hover:border-khata hover:text-khata"
                >
                  {shortCitation(ref)}
                </button>
              ) : (
                <span key={ref} className="rounded border border-rule bg-sheet-2 px-1.5 text-xs font-semibold">
                  {shortCitation(ref)}
                </span>
              );
            })}
          </p>
        ) : null}

        {request.triggers.length ? (
          <p className="mt-2 text-sm text-ink-2">Why a person decides: {request.triggers.join("; ")}.</p>
        ) : null}

        {decision?.note ? <p className="mt-3 text-sm">Note: {decision.note}</p> : null}

        {!decided ? (
          <div className="mt-5 border-t border-rule pt-4">
            {!allowed ? (
              <Notice tone="info">
                {login.role === "admin"
                  ? "Admins can see every shop's approvals, but a shop's own people decide. Log in as this shop's owner to decide."
                  : "This needs the owner. You are logged in as staff, so ask the owner to open Approvals."}
              </Notice>
            ) : (
              <>
                <label className="block text-sm font-medium">
                  Note <span className="font-normal text-ink-3">(optional, saved with the decision)</span>
                  <input
                    value={note}
                    maxLength={500}
                    onChange={(e) => setNote(e.target.value)}
                    className="mt-1 block w-full rounded-md border border-rule bg-sheet px-3 py-1.5 font-normal"
                  />
                </label>
                {error ? (
                  <div className="mt-3">
                    <Notice title="The decision was not saved">{error}</Notice>
                  </div>
                ) : null}
                <div className="mt-4 flex flex-wrap gap-2">
                  {single ? (
                    choices[single.approval_id] === "modify" ? (
                      <>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => send()}
                          className="rounded-md bg-stamp px-4 py-2 font-semibold text-sheet hover:opacity-90 disabled:opacity-50"
                        >
                          {busy ? "Saving…" : "Approve with changes"}
                        </button>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => setChoices({ [single.approval_id]: "approve" })}
                          className="rounded-md border border-rule px-4 py-2 font-medium text-ink-2 hover:text-ink"
                        >
                          Cancel changes
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => send("approve")}
                          className="rounded-md bg-stamp px-4 py-2 font-semibold text-sheet hover:opacity-90 disabled:opacity-50"
                        >
                          {busy === "approve" ? "Approving…" : "Approve"}
                        </button>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => setChoices({ [single.approval_id]: "modify" })}
                          className="rounded-md border border-ink px-4 py-2 font-semibold text-ink hover:bg-sheet-2 disabled:opacity-50"
                        >
                          Modify
                        </button>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => send("reject")}
                          className="rounded-md border border-khata px-4 py-2 font-semibold text-khata hover:bg-khata-soft disabled:opacity-50"
                        >
                          {busy === "reject" ? "Rejecting…" : "Reject"}
                        </button>
                      </>
                    )
                  ) : (
                    <button
                      type="button"
                      disabled={busy !== null}
                      onClick={() => send()}
                      className="rounded-md bg-ink px-4 py-2 font-semibold text-sheet hover:opacity-90 disabled:opacity-50"
                    >
                      {busy ? "Saving…" : "Send these decisions"}
                    </button>
                  )}
                </div>
                <p className="mt-2 text-xs text-ink-3">
                  ORM_AI only does what you approve, and reports only what its tools confirm.
                </p>
              </>
            )}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function ArgumentEditor({
  action,
  draft,
  onChange,
}: {
  action: ApprovalActionView;
  draft: Record<string, string>;
  onChange: (draft: Record<string, string>) => void;
}) {
  const set = (key: string, value: string) => onChange({ ...draft, [key]: value });
  const field = "mt-1 block w-full rounded-md border border-rule-strong bg-sheet px-3 py-1.5 font-normal";
  return (
    <div className="mt-3 space-y-3 text-sm">
      {Object.entries(action.arguments).map(([key, value]) => {
        const id = `${action.approval_id}-${key}`;
        if (key.endsWith("_id")) {
          return (
            <p key={key}>
              <span className="text-ink-3">{argumentLabel(key)}:</span> <RecordId>{showValue(value)}</RecordId>
              <span className="ml-2 text-xs text-ink-3">(a change cannot switch to another record)</span>
            </p>
          );
        }
        if (isLines(value)) {
          return (
            <fieldset key={key}>
              <legend className="font-medium">{argumentLabel(key)}</legend>
              {value.map((line, i) => (
                <label key={line.product_id} className="mt-1 flex items-center gap-3">
                  <RecordId>{line.product_id}</RecordId>
                  <input
                    type="number"
                    min={1}
                    step={1}
                    value={draft[`lines.${i}`] ?? ""}
                    onChange={(e) => set(`lines.${i}`, e.target.value)}
                    className="w-28 rounded-md border border-rule-strong bg-sheet px-2 py-1"
                    aria-label={`Quantity of ${line.product_id}`}
                  />
                </label>
              ))}
            </fieldset>
          );
        }
        if (typeof value === "boolean") {
          return (
            <label key={key} className="flex items-center gap-2 font-medium">
              <input type="checkbox" checked={draft[key] === "true"} onChange={(e) => set(key, e.target.checked ? "true" : "false")} />
              {argumentLabel(key)}
            </label>
          );
        }
        if (CHOICES[key]) {
          return (
            <label key={key} htmlFor={id} className="block font-medium">
              {argumentLabel(key)}
              <select id={id} value={draft[key]} onChange={(e) => set(key, e.target.value)} className={field}>
                {CHOICES[key].map((option) => (
                  <option key={option} value={option}>
                    {option.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </label>
          );
        }
        if (typeof value === "number" || MONEY.has(key)) {
          return (
            <label key={key} htmlFor={id} className="block font-medium">
              {argumentLabel(key)}
              <input
                id={id}
                type="number"
                step={INTEGER.has(key) ? 1 : MONEY.has(key) ? 0.01 : "any"}
                value={draft[key]}
                onChange={(e) => set(key, e.target.value)}
                className={`${field} max-w-48`}
              />
            </label>
          );
        }
        return (
          <label key={key} htmlFor={id} className="block font-medium">
            {argumentLabel(key)}
            {LONG_TEXT.has(key) ? (
              <textarea id={id} rows={2} value={draft[key]} onChange={(e) => set(key, e.target.value)} className={field} />
            ) : (
              <input id={id} value={draft[key]} onChange={(e) => set(key, e.target.value)} className={field} />
            )}
          </label>
        );
      })}
    </div>
  );
}
