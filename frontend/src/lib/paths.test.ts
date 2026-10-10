// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";

import { safeNext } from "./paths.ts";

test("paths on this site are kept", () => {
  assert.equal(safeNext("/approvals"), "/approvals");
  assert.equal(safeNext("/chat?session=abc"), "/chat?session=abc");
});

test("anything that could leave the site goes to the chat instead", () => {
  for (const bad of [null, "", "https://evil.example", "//evil.example", "/\\evil.example", "\\\\evil", "javascript:alert(1)"]) {
    assert.equal(safeNext(bad), "/chat", String(bad));
  }
});
