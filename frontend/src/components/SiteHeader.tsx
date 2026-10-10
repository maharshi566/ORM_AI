"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { listApprovals, logout } from "@/lib/api";
import { ROLE_LABEL, useLogin } from "@/lib/login";

const NAV = [
  { href: "/chat", label: "Chat" },
  { href: "/approvals", label: "Approvals" },
  { href: "/admin", label: "Admin" },
] as const;

/** How many approvals wait in the logged-in user's shop; refreshed on every page change. */
function usePendingCount(loggedIn: boolean, pathname: string): number | null {
  const [pending, setPending] = useState<number | null>(null);
  useEffect(() => {
    if (!loggedIn) return;
    const controller = new AbortController();
    listApprovals({ status: "pending", limit: 1 }, controller.signal).then(
      (result) => setPending(result.counts.pending ?? 0),
      () => setPending(null),
    );
    const onDecided = () => {
      listApprovals({ status: "pending", limit: 1 }).then(
        (result) => setPending(result.counts.pending ?? 0),
        () => undefined,
      );
    };
    window.addEventListener("orm-ai:approvals-changed", onDecided);
    return () => {
      controller.abort();
      window.removeEventListener("orm-ai:approvals-changed", onDecided);
    };
  }, [loggedIn, pathname]);
  return loggedIn ? pending : null;
}

export function SiteHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const login = useLogin();
  const pending = usePendingCount(Boolean(login), pathname);

  return (
    <header className="border-b border-rule bg-sheet lg:h-16">
      <div className="mx-auto flex w-full max-w-[1440px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 sm:px-6 lg:h-full lg:flex-nowrap lg:py-0">
        <Link href="/" className="flex items-center gap-2.5" aria-label="ORM_AI home">
          <span aria-hidden className="h-7 w-1.5 rounded-sm bg-khata" />
          <span className="font-serif text-lg font-extrabold tracking-tight">ORM_AI</span>
        </Link>

        <nav aria-label="Main" className="flex gap-1 text-[15px]">
          {NAV.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`relative rounded-md px-3 py-1.5 font-medium transition-colors ${
                  active ? "bg-paper text-ink" : "text-ink-2 hover:bg-paper hover:text-ink"
                }`}
              >
                {item.label}
                {item.href === "/approvals" && pending ? (
                  <span className="ml-1.5 inline-flex min-w-5 items-center justify-center rounded-full bg-khata px-1.5 text-xs font-semibold leading-5 text-sheet">
                    {pending}
                    <span className="sr-only"> waiting</span>
                  </span>
                ) : null}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3 text-sm">
          {login ? (
            <>
              <div className="text-right leading-tight">
                <div className="font-semibold">
                  {login.name ?? login.userId}
                  <span className="font-normal text-ink-3"> ({ROLE_LABEL[login.role].toLowerCase()})</span>
                </div>
                <div className="text-ink-2">{login.shopName ?? "Every shop"}</div>
              </div>
              <button
                type="button"
                onClick={() => {
                  logout();
                  router.push("/login");
                }}
                className="rounded-md border border-rule px-2.5 py-1 text-ink-2 hover:border-rule-strong hover:text-ink"
              >
                Log out
              </button>
            </>
          ) : login === null ? (
            <Link href="/login" className="rounded-md bg-ink px-3 py-1.5 font-medium text-sheet hover:opacity-90">
              Log in
            </Link>
          ) : null}
        </div>
      </div>
    </header>
  );
}
