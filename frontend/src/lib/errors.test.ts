// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";

import { errorReport, MAX_REPORTS_PER_PAGE } from "./errors.ts";

test("a report carries the message, the page without its query, and the stack", () => {
  const report = errorReport("render", new Error("Cannot read 'name'"), "/chat?session=abc", new Set());
  assert.equal(report?.message, "Cannot read 'name'");
  assert.equal(report?.page, "/chat");
  assert.match(report?.stack ?? "", /Cannot read/);
});

test("the same error is sent once, and at most five errors per page", () => {
  const seen = new Set<string>();
  assert.ok(errorReport("error", "boom", "/", seen));
  assert.equal(errorReport("error", "boom", "/", seen), null);
  for (let i = 0; i < MAX_REPORTS_PER_PAGE * 2; i += 1) errorReport("error", `boom ${i}`, "/", seen);
  assert.equal(seen.size, MAX_REPORTS_PER_PAGE);
});

test("odd values still give a readable report", () => {
  assert.equal(errorReport("unhandledrejection", undefined, "/", new Set())?.message, "Unknown error");
  assert.equal(errorReport("error", "x".repeat(900), "/", new Set())?.message.length, 500);
});
