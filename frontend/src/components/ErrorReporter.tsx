"use client";

import { useEffect } from "react";

import { API_URL } from "@/lib/api";
import { type ClientErrorKind, errorReport } from "@/lib/errors";

const seen = new Set<string>();

/** Send an error to the backend; a report that cannot be sent is dropped quietly. */
export function reportError(kind: ClientErrorKind, error: unknown): void {
  if (typeof window === "undefined") return;
  const report = errorReport(kind, error, window.location.pathname, seen);
  if (!report) return;
  try {
    void fetch(`${API_URL}/api/client-errors`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(report),
      keepalive: true, // still sent when the page is closing
    }).catch(() => undefined);
  } catch {
    // reporting must never cause a second error
  }
}

/** Reports errors no component caught: uncaught exceptions and rejected promises. */
export function ErrorReporter() {
  useEffect(() => {
    const onError = (event: ErrorEvent) => reportError("error", event.error ?? event.message);
    const onRejection = (event: PromiseRejectionEvent) => reportError("unhandledrejection", event.reason);
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);
  return null;
}
