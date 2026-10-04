import Link from "next/link";

import { BackendStatus } from "@/components/BackendStatus";

const PAGES = [
  {
    href: "/chat",
    title: "Chat",
    phase: "Phase 7",
    text: "Ask about stock, sales, suppliers and customer credit, with sources and tool activity beside each answer.",
  },
  {
    href: "/approvals",
    title: "Approvals",
    phase: "Phase 7",
    text: "Approve, reject or edit purchase orders, price changes and credit decisions before ORM_AI acts.",
  },
  {
    href: "/admin",
    title: "Admin",
    phase: "Phase 7",
    text: "Workflow runs, errors, latency, tool calls and evaluation scores.",
  },
];

export default function Home() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 sm:px-6">
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Organised records for local shops</h1>
      <p className="mt-3 max-w-2xl text-stone-600 dark:text-stone-400">
        ORM_AI helps shopkeepers keep stock, sales, supplier orders and customer credit organised and detailed. Specialist
        AI agents look up the records, check the shop&apos;s rules and ask before doing anything that moves money or stock.
      </p>

      <div className="mt-10">
        <BackendStatus />
      </div>

      <h2 className="mt-12 text-sm font-semibold uppercase tracking-wider text-stone-500">Coming next</h2>
      <ul className="mt-4 grid gap-4 sm:grid-cols-3">
        {PAGES.map((page) => (
          <li key={page.href}>
            <Link
              href={page.href}
              className="block h-full rounded-xl border border-stone-200 p-5 transition hover:border-emerald-500 dark:border-stone-800 dark:hover:border-emerald-500"
            >
              <span className="flex items-center justify-between">
                <span className="font-semibold">{page.title}</span>
                <span className="text-xs text-stone-500">{page.phase}</span>
              </span>
              <span className="mt-2 block text-sm text-stone-600 dark:text-stone-400">{page.text}</span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
