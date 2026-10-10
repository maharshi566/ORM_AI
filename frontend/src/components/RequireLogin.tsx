"use client";

import { useRouter } from "next/navigation";
import { Fragment, type ReactNode } from "react";

import { type Login, useLogin } from "@/lib/login";

/** Shows the page only to a logged-in user; everyone else gets a way to log in. */
export function RequireLogin({ children }: { children: (login: Login) => ReactNode }) {
  const login = useLogin();
  const router = useRouter();

  if (login === undefined) {
    return <div className="flex-1" aria-busy="true" />;
  }
  if (login === null) {
    return (
      <div className="mx-auto w-full max-w-xl px-4 py-16 sm:px-6">
        <h1 className="font-serif text-2xl font-extrabold">Log in to continue</h1>
        <p className="mt-3 text-ink-2">
          ORM_AI shows each person their own shop&apos;s records, so it needs to know who you are first.
        </p>
        <button
          type="button"
          onClick={() => router.push(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`)}
          className="mt-6 inline-block rounded-md bg-ink px-4 py-2 font-medium text-sheet hover:opacity-90"
        >
          Log in
        </button>
      </div>
    );
  }
  // Keyed by user: logging in as someone else (here or in another tab) starts the page afresh.
  return <Fragment key={login.userId}>{children(login)}</Fragment>;
}
