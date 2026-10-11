// What the website reports when a visitor hits an error (sent by components/ErrorReporter
// to POST /api/client-errors; docs/monitoring.md). The same error is sent once per page
// load, and at most five errors per page load. No imports, so it runs under `npm test`.

export type ClientErrorKind = "error" | "unhandledrejection" | "render";

export type ClientErrorReport = { kind: ClientErrorKind; message: string; page: string; stack: string };

export const MAX_REPORTS_PER_PAGE = 5;

/** The report to send, or null when this error was already sent or the cap is reached. */
export function errorReport(kind: ClientErrorKind, error: unknown, page: string, seen: Set<string>): ClientErrorReport | null {
  const text = error instanceof Error ? error.message : String(error ?? "");
  const message = text.slice(0, 500) || "Unknown error";
  const key = `${kind}:${message}`;
  if (seen.has(key) || seen.size >= MAX_REPORTS_PER_PAGE) return null;
  seen.add(key);
  const stack = error instanceof Error && error.stack ? error.stack.slice(0, 4000) : "";
  return { kind, message, page: page.split("?")[0].slice(0, 300), stack };
}
