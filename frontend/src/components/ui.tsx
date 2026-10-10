// Small building blocks shared by the pages.

import type { ReactNode } from "react";

import { statusLabel, TONE_CLASS } from "@/lib/format";

export function StatusBadge({ status, label }: { status: string | null | undefined; label?: string }) {
  const { label: text, tone } = statusLabel(status);
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${TONE_CLASS[tone]}`}>
      {label ?? text}
    </span>
  );
}

export function Notice({
  tone = "problem",
  title,
  children,
  action,
}: {
  tone?: "problem" | "info" | "done";
  title?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  const styles = {
    problem: "border-amber/40 bg-amber-soft",
    info: "border-rule bg-sheet-2",
    done: "border-stamp/40 bg-stamp-soft",
  }[tone];
  return (
    <div role={tone === "problem" ? "alert" : "status"} className={`rounded-lg border px-4 py-3 text-sm ${styles}`}>
      {title ? <p className="font-semibold">{title}</p> : null}
      <div className={title ? "mt-0.5 text-ink-2" : "text-ink"}>{children}</div>
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function PageTitle({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="max-w-3xl">
      <h1 className="font-serif text-[1.75rem] font-extrabold leading-tight tracking-tight">{title}</h1>
      {children ? <p className="mt-2 text-ink-2">{children}</p> : null}
    </div>
  );
}

export function SectionTitle({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="font-serif text-lg font-bold">{title}</h2>
      {children ? <div className="text-sm text-ink-2">{children}</div> : null}
    </div>
  );
}

/** A record ID such as SALE-005598, set so the digits are easy to compare. */
export function RecordId({ children }: { children: ReactNode }) {
  return <span className="font-medium tabular-nums tracking-wide">{children}</span>;
}

export function Spinner({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-ink-2" role="status">
      <span aria-hidden className="h-3 w-3 animate-spin rounded-full border-2 border-rule-strong border-t-khata" />
      {label}
    </span>
  );
}
