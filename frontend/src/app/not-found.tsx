import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto w-full max-w-xl px-4 py-16 sm:px-6">
      <h1 className="font-serif text-2xl font-extrabold">This page is not in the ledger</h1>
      <p className="mt-3 text-ink-2">The address may be mistyped, or the page was moved.</p>
      <Link href="/chat" className="mt-6 inline-block rounded-md bg-ink px-4 py-2 font-medium text-sheet hover:opacity-90">
        Go to the chat
      </Link>
    </div>
  );
}
