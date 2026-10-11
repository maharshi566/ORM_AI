// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";

import { ingestSummary, skippedUploads, uploadProblem } from "./uploads.ts";

test("markdown, text and PDF files up to 5 MB can be uploaded", () => {
  assert.equal(uploadProblem("diwali-hours.md", 87), null);
  assert.equal(uploadProblem("Returns Policy.PDF", 4_999_999), null);
  assert.equal(uploadProblem("notes.txt", 1), null);
});

test("other files are refused before anything is sent", () => {
  assert.match(uploadProblem("setup.exe", 10) ?? "", /Only \.md, \.txt and \.pdf/);
  assert.match(uploadProblem("README", 10) ?? "", /Only/);
  assert.match(uploadProblem("archive.md.zip", 10) ?? "", /Only/);
  assert.equal(uploadProblem("empty.md", 0), "This file is empty.");
  assert.equal(uploadProblem("big.pdf", 7_250_000), "This file is 7.3 MB; the limit is 5 MB.");
});

test("an ingestion summary is shown in a fixed order, without unknown keys", () => {
  const summary = { removed: 0, chunks: 412, documents: 79, added: 3, unchanged: 409, skipped_uploads: [], took_s: 4.2 };
  assert.deepEqual(ingestSummary(summary), [
    ["Documents", 79],
    ["Passages", 412],
    ["New", 3],
    ["Unchanged", 409],
    ["Removed", 0],
  ]);
  assert.deepEqual(ingestSummary({}), []);
});

test("skipped uploads are listed as text", () => {
  assert.deepEqual(skippedUploads({ skipped_uploads: ["UPL-SHOP-002-ab12: no text", { id: "x" }] }), [
    "UPL-SHOP-002-ab12: no text",
    '{"id":"x"}',
  ]);
  assert.deepEqual(skippedUploads({}), []);
});
