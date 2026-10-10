// Where to go after logging in. Pure, so it is tested on its own (paths.test.ts).

/** Only a path on this site, never another address ("//evil.example", "/\\evil.example"). */
export function safeNext(next: string | null | undefined, fallback = "/chat"): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) return fallback;
  return next;
}
