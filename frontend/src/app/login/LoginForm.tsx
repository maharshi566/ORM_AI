"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { Notice, PageTitle, Spinner } from "@/components/ui";
import { ApiError, devLogin, errorMessage, getDevUsers, tokenLogin } from "@/lib/api";
import { ROLE_LABEL } from "@/lib/login";
import { safeNext } from "@/lib/paths";
import type { DevUser } from "@/types/api";

// The people the guides use in their examples come first.
const SUGGESTED = ["USR-001", "USR-002", "USR-003", "USR-101"];
const NOTE: Record<string, string> = {
  "USR-001": "Can approve everything in this shop",
  "USR-002": "Can approve small refunds and reminders",
  "USR-003": "A second shop, for checking that shops stay apart",
  "USR-101": "Sees every shop, the admin page and evaluation scores",
};

type Users = { kind: "loading" } | { kind: "ready"; users: DevUser[] } | { kind: "unavailable"; message: string };


export function LoginForm() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [users, setUsers] = useState<Users>({ kind: "loading" });
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    getDevUsers(controller.signal).then(
      (list) => setUsers({ kind: "ready", users: list }),
      (err: unknown) => {
        if (controller.signal.aborted) return;
        const message =
          err instanceof ApiError && err.status === 403
            ? "This server does not offer demo logins. Use a token from your login system below."
            : errorMessage(err);
        setUsers({ kind: "unavailable", message });
      },
    );
    return () => controller.abort();
  }, []);

  const shops = useMemo(() => {
    if (users.kind !== "ready") return [];
    const words = filter.trim().toLowerCase();
    const matching = users.users.filter((u) =>
      !words ? true : [u.name, u.user_id, u.shop_id ?? "", u.shop_name ?? "", u.role].join(" ").toLowerCase().includes(words),
    );
    const groups = new Map<string, DevUser[]>();
    for (const user of matching) {
      const key = user.shop_id ?? "every shop";
      groups.set(key, [...(groups.get(key) ?? []), user]);
    }
    return [...groups.entries()];
  }, [users, filter]);

  async function pick(userId: string) {
    setBusy(userId);
    setError(null);
    try {
      await devLogin(userId);
      router.push(next);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(null);
    }
  }

  async function submitToken(event: React.FormEvent) {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy("token");
    setError(null);
    try {
      await tokenLogin(token);
      router.push(next);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(null);
    }
  }

  const suggested = users.kind === "ready" ? SUGGESTED.map((id) => users.users.find((u) => u.user_id === id)).filter(Boolean) : [];

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-10 sm:px-6">
      <PageTitle title="Log in">
        Pick who you are. Each shopkeeper sees only their own shop&apos;s records; staff can approve small things, the
        owner everything.
      </PageTitle>

      {error ? (
        <div className="mt-6">
          <Notice title="Could not log in">{error}</Notice>
        </div>
      ) : null}

      {users.kind === "loading" ? (
        <div className="mt-8">
          <Spinner label="Asking the server for its demo users" />
        </div>
      ) : null}

      {users.kind === "unavailable" ? (
        <div className="mt-8">
          <Notice tone="info">{users.message}</Notice>
        </div>
      ) : null}

      {users.kind === "ready" ? (
        <>
          <section className="mt-8" aria-labelledby="suggested">
            <h2 id="suggested" className="font-serif text-lg font-bold">
              For the demo
            </h2>
            <ul className="mt-3 divide-y divide-rule overflow-hidden rounded-lg border border-rule bg-sheet">
              {suggested.map((user) =>
                user ? (
                  <li key={user.user_id}>
                    <button
                      type="button"
                      disabled={busy !== null}
                      onClick={() => pick(user.user_id)}
                      className="flex w-full flex-wrap items-center gap-x-4 gap-y-0.5 px-4 py-3 text-left hover:bg-sheet-2 disabled:opacity-60"
                    >
                      <span className="min-w-48 font-semibold">
                        {user.name} <span className="font-normal text-ink-3">({ROLE_LABEL[user.role].toLowerCase()})</span>
                      </span>
                      <span className="flex-1 text-sm text-ink-2">
                        {user.shop_name ?? "Every shop"} <span className="text-ink-3">{user.shop_id}</span>
                        <span className="block text-ink-3">{NOTE[user.user_id]}</span>
                      </span>
                      <span className="text-sm font-medium text-khata">{busy === user.user_id ? "Logging in…" : "Log in"}</span>
                    </button>
                  </li>
                ) : null,
              )}
            </ul>
          </section>

          <section className="mt-10" aria-labelledby="everyone">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <h2 id="everyone" className="font-serif text-lg font-bold">
                All {users.users.length} demo users
              </h2>
              <label className="flex items-center gap-2 text-sm text-ink-2">
                Find
                <input
                  type="search"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder="Name, shop or ID"
                  className="w-56 rounded-md border border-rule bg-sheet px-3 py-1.5 text-ink placeholder:text-ink-3"
                />
              </label>
            </div>
            <div className="mt-3 max-h-[28rem] overflow-y-auto rounded-lg border border-rule bg-sheet">
              {shops.length === 0 ? <p className="px-4 py-6 text-sm text-ink-2">No one matches “{filter}”.</p> : null}
              {shops.map(([shop, people]) => (
                <div key={shop} className="border-b border-rule last:border-b-0">
                  <p className="bg-sheet-2 px-4 py-1.5 text-sm text-ink-2">
                    <span className="font-semibold text-ink">{people[0].shop_name ?? "Admins"}</span> {people[0].shop_id ?? ""}
                  </p>
                  <ul>
                    {people.map((user) => (
                      <li key={user.user_id}>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => pick(user.user_id)}
                          className="flex w-full items-center justify-between px-4 py-2 text-left text-sm hover:bg-sheet-2 disabled:opacity-60"
                        >
                          <span>
                            {user.name} <span className="text-ink-3">({ROLE_LABEL[user.role].toLowerCase()})</span>
                          </span>
                          <span className="text-ink-3">{busy === user.user_id ? "Logging in…" : user.user_id}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </section>
        </>
      ) : null}

      <details className="mt-10 rounded-lg border border-rule bg-sheet px-4 py-3" open={users.kind === "unavailable"}>
        <summary className="cursor-pointer font-medium">Log in with a token instead</summary>
        <form onSubmit={submitToken} className="mt-3 space-y-3">
          <p className="text-sm text-ink-2">
            On a shared server, demo logins are switched off. Paste the token your login system issued (signed with the
            server&apos;s AUTH_SECRET).
          </p>
          <label className="block text-sm font-medium">
            Token
            <textarea
              value={token}
              onChange={(e) => setToken(e.target.value)}
              rows={3}
              spellCheck={false}
              autoComplete="off"
              className="mt-1 block w-full rounded-md border border-rule bg-sheet-2 px-3 py-2 font-normal break-all text-ink"
            />
          </label>
          <button
            type="submit"
            disabled={busy !== null || !token.trim()}
            className="rounded-md bg-ink px-4 py-2 font-medium text-sheet hover:opacity-90 disabled:opacity-50"
          >
            {busy === "token" ? "Checking…" : "Log in with this token"}
          </button>
        </form>
      </details>
    </div>
  );
}
