import Link from "next/link";

import { BackendStatus } from "@/components/BackendStatus";

const STEPS = [
  {
    title: "Ask in plain words",
    text: "“The customer brought back SALE-005598 today. Please process the return.” Stock, sales, supplier orders, customer credit.",
  },
  {
    title: "The agents look it up",
    text: "One reads the shop's records, another its rules, a third weighs the evidence. Each answer cites the rule it used.",
  },
  {
    title: "You decide what changes",
    text: "Refunds, price changes, stock write-offs and supplier messages wait on an approval slip until staff or the owner says yes.",
  },
];

export default function Home() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 sm:px-6 sm:py-16">
      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
        <div>
          <h1 className="max-w-[18ch] font-serif text-4xl font-extrabold leading-[1.15] tracking-tight sm:text-5xl">
            A shop ledger that answers back
          </h1>
          <p className="mt-5 max-w-[58ch] text-lg text-ink-2">
            ORM_AI keeps a local shop&apos;s stock, sales, supplier orders and customer credit (udhaar) organised.
            Specialist AI agents look up the records and the shop&apos;s rules, and nothing that moves money or stock
            happens until a person approves it.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link href="/chat" className="rounded-md bg-khata px-5 py-2.5 font-semibold text-sheet hover:opacity-90">
              Open the chat
            </Link>
            <Link href="/approvals" className="rounded-md border border-ink px-5 py-2.5 font-semibold hover:bg-sheet">
              See what waits for approval
            </Link>
          </div>
        </div>
        <BackendStatus />
      </div>

      <ol className="mt-16 grid gap-px overflow-hidden rounded-lg border border-rule bg-rule sm:grid-cols-3">
        {STEPS.map((step, index) => (
          <li key={step.title} className="bg-sheet p-5">
            <p className="font-serif text-sm font-bold text-khata">{index + 1}</p>
            <h2 className="mt-1 font-serif text-lg font-bold">{step.title}</h2>
            <p className="mt-2 text-[15px] text-ink-2">{step.text}</p>
          </li>
        ))}
      </ol>

      <p className="mt-10 max-w-[70ch] text-sm text-ink-3">
        The demo runs on 50 synthetic shops with 91 days of records. Every answer and decision is kept: the{" "}
        <Link href="/admin" className="underline underline-offset-2">
          admin page
        </Link>{" "}
        shows each agent&apos;s steps, the tools it called and the evaluation scores.
      </p>
    </div>
  );
}
