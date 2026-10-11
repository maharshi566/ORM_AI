"use client";

import { useEffect } from "react";

import { reportError } from "@/components/ErrorReporter";

/** The whole layout crashed: a bare page (the app's styles may not have loaded). */
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    reportError("render", error);
  }, [error]);

  return (
    <html lang="en-IN">
      <body style={{ fontFamily: "system-ui, sans-serif", margin: "4rem auto", maxWidth: "36rem", padding: "0 1rem" }}>
        <h1>ORM_AI could not load</h1>
        <p>The problem was reported. Nothing in your shop&apos;s records was changed.</p>
        <button type="button" onClick={reset}>
          Try again
        </button>
      </body>
    </html>
  );
}
