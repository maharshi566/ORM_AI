"use client";

import { useEffect } from "react";

import { reportError } from "@/components/ErrorReporter";

/** A page that crashed while rendering: say so plainly, report it, offer a retry. */
export default function PageError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    reportError("render", error);
  }, [error]);

  return (
    <div className="mx-auto w-full max-w-xl px-4 py-16 sm:px-6">
      <h1 className="font-serif text-2xl font-extrabold">Something went wrong on this page</h1>
      <p className="mt-3 text-ink-2">
        The problem was reported. Nothing in your shop&apos;s records was changed. Try again, or go back to the chat.
      </p>
      <div className="mt-6 flex flex-wrap gap-3">
        <button type="button" onClick={reset} className="rounded-md bg-ink px-4 py-2 font-medium text-sheet hover:opacity-90">
          Try again
        </button>
        <a href="/chat" className="rounded-md border border-rule px-4 py-2 font-medium text-ink-2 hover:text-ink">
          Go to the chat
        </a>
      </div>
    </div>
  );
}
