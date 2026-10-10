"use client";

import { useEffect, useState } from "react";

import { API_URL, errorMessage, getHealth } from "@/lib/api";
import type { HealthResponse } from "@/types/api";

type Status =
  | { kind: "loading" }
  | { kind: "ok" | "degraded"; health: HealthResponse; checkedAt: Date }
  | { kind: "unreachable"; message: string; checkedAt: Date };

const BADGE: Record<Status["kind"], { label: string; className: string }> = {
  loading: { label: "Checking", className: "bg-sheet-2 text-ink-2" },
  ok: { label: "Running", className: "bg-stamp-soft text-stamp" },
  degraded: { label: "Partly down", className: "bg-amber-soft text-amber" },
  unreachable: { label: "Not reachable", className: "bg-khata-soft text-khata" },
};

const LABELS: Record<string, string> = { database: "Records (PostgreSQL)", redis: "Conversation memory (Redis)" };

/** Whether the backend and the services it needs are up (GET /api/health). */
export function BackendStatus() {
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal).then(
      (health) => setStatus({ kind: health.status, health, checkedAt: new Date() }),
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setStatus({ kind: "unreachable", message: errorMessage(error), checkedAt: new Date() });
      },
    );
    return () => controller.abort();
  }, [attempt]);

  const badge = BADGE[status.kind];

  return (
    <section aria-labelledby="backend-status" className="rounded-lg border border-rule bg-sheet p-5">
      <div className="flex items-center justify-between gap-3">
        <h2 id="backend-status" className="font-serif text-base font-bold">
          The ORM_AI server
        </h2>
        <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${badge.className}`}>{badge.label}</span>
      </div>

      {status.kind === "ok" || status.kind === "degraded" ? (
        <ul className="mt-4 divide-y divide-rule text-sm">
          {Object.entries(status.health.checks).map(([name, dependency]) => (
            <li key={name} className="flex items-center justify-between gap-3 py-2">
              <span className="flex items-center gap-2">
                <span aria-hidden className={`h-2 w-2 rounded-full ${dependency.status === "ok" ? "bg-stamp" : "bg-khata"}`} />
                {LABELS[name] ?? name}
              </span>
              <span className="text-ink-3 tabular-nums">
                {dependency.status === "ok" ? `${dependency.latency_ms ?? "–"} ms` : (dependency.error ?? "down")}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {status.kind === "unreachable" ? (
        <p className="mt-4 text-sm text-ink-2">
          {status.message} Start it from the <code className="rounded bg-sheet-2 px-1">backend</code> folder with{" "}
          <code className="rounded bg-sheet-2 px-1">uvicorn app.main:app --reload</code>.
        </p>
      ) : null}

      <div className="mt-4 flex items-center justify-between gap-3 text-xs text-ink-3">
        <span className="min-w-0 break-all">{status.kind === "loading" ? "Asking…" : API_URL}</span>
        <button
          type="button"
          onClick={() => {
            setStatus({ kind: "loading" });
            setAttempt((n) => n + 1);
          }}
          className="shrink-0 rounded-md border border-rule px-2.5 py-1 font-medium text-ink-2 hover:text-ink"
        >
          Check again
        </button>
      </div>
    </section>
  );
}
