"use client";

import { useEffect, useState } from "react";

import { API_URL, getHealth } from "@/lib/api";
import type { HealthResponse } from "@/types/api";

type Status =
  | { kind: "loading" }
  | { kind: "ok" | "degraded"; health: HealthResponse; checkedAt: Date }
  | { kind: "unreachable"; message: string; checkedAt: Date };

const BADGE: Record<Status["kind"], { label: string; className: string }> = {
  loading: { label: "Checking…", className: "bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300" },
  ok: { label: "All systems up", className: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300" },
  degraded: { label: "Degraded", className: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300" },
  unreachable: { label: "Backend unreachable", className: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300" },
};

const LABELS: Record<string, string> = { database: "PostgreSQL", redis: "Redis" };

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}

export function BackendStatus() {
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal).then(
      (health) => setStatus({ kind: health.status, health, checkedAt: new Date() }),
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setStatus({ kind: "unreachable", message: describeError(error), checkedAt: new Date() });
      },
    );
    return () => controller.abort();
  }, [attempt]);

  const badge = BADGE[status.kind];

  return (
    <section className="rounded-xl border border-stone-200 bg-white p-6 dark:border-stone-800 dark:bg-stone-900">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">Backend status</h2>
          <p className="mt-1 font-mono text-xs text-stone-500 break-all">{API_URL}/api/health</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs font-medium ${badge.className}`}>{badge.label}</span>
      </div>

      {status.kind === "ok" || status.kind === "degraded" ? (
        <ul className="mt-5 divide-y divide-stone-100 dark:divide-stone-800">
          {Object.entries(status.health.checks).map(([name, dependency]) => (
            <li key={name} className="flex items-center justify-between py-2.5 text-sm">
              <span className="flex items-center gap-2">
                <span
                  aria-hidden
                  className={`h-2 w-2 rounded-full ${dependency.status === "ok" ? "bg-emerald-500" : "bg-red-500"}`}
                />
                {LABELS[name] ?? name}
              </span>
              <span className="text-stone-500">
                {dependency.status === "ok" ? `${dependency.latency_ms ?? "–"} ms` : dependency.error}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {status.kind === "unreachable" ? (
        <p className="mt-5 text-sm text-stone-600 dark:text-stone-400">
          Could not reach the API ({status.message}). Start it with{" "}
          <code className="rounded bg-stone-100 px-1.5 py-0.5 text-xs dark:bg-stone-800">uvicorn app.main:app --reload</code>{" "}
          from the <code className="rounded bg-stone-100 px-1.5 py-0.5 text-xs dark:bg-stone-800">backend</code> folder.
        </p>
      ) : null}

      <div className="mt-5 flex items-center justify-between text-xs text-stone-500">
        <span>
          {status.kind === "loading"
            ? "Contacting the API…"
            : `Checked at ${status.checkedAt.toLocaleTimeString()}`}
          {status.kind === "ok" || status.kind === "degraded"
            ? ` · ${status.health.app} ${status.health.version} (${status.health.environment})`
            : ""}
        </span>
        <button
          type="button"
          onClick={() => {
            setStatus({ kind: "loading" });
            setAttempt((n) => n + 1);
          }}
          className="rounded-md border border-stone-300 px-3 py-1.5 font-medium text-stone-700 hover:bg-stone-50 dark:border-stone-700 dark:text-stone-200 dark:hover:bg-stone-800"
        >
          Check again
        </button>
      </div>
    </section>
  );
}
