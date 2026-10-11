// What may be uploaded to the knowledge base, checked before the request is sent. The
// backend checks everything again (size, that the bytes match the extension, that the
// text can be read). Pure, so it is tested on its own (uploads.test.ts).

export const MAX_UPLOAD_BYTES = 5_000_000; // UPLOAD_MAX_MB in the backend
export const UPLOAD_ACCEPT = ".md,.txt,.pdf";

export const CATEGORY_LABEL = {
  sop: "Procedure",
  policy: "Shop rule",
  faq: "Questions and answers",
  supplier_terms: "Supplier terms",
  shop_profile: "About the shop",
  supplier_flyer: "Supplier flyer",
} as const;

/** Why this file cannot be uploaded, or null when it can. */
export function uploadProblem(name: string, size: number): string | null {
  const extension = name.includes(".") ? name.slice(name.lastIndexOf(".")).toLowerCase() : "";
  if (![".md", ".txt", ".pdf"].includes(extension)) return "Only .md, .txt and .pdf files can be added.";
  if (size === 0) return "This file is empty.";
  if (size > MAX_UPLOAD_BYTES) return `This file is ${(size / 1_000_000).toFixed(1)} MB; the limit is 5 MB.`;
  return null;
}

/** An ingestion summary as (label, value) pairs, in a fixed order; unknown keys are left out. */
export function ingestSummary(summary: Record<string, unknown>): [string, number][] {
  const fields: [string, string][] = [
    ["documents", "Documents"],
    ["chunks", "Passages"],
    ["added", "New"],
    ["unchanged", "Unchanged"],
    ["removed", "Removed"],
  ];
  return fields.filter(([key]) => typeof summary[key] === "number").map(([key, label]) => [label, summary[key] as number]);
}

/** The uploads ingestion could not use, as readable strings. */
export function skippedUploads(summary: Record<string, unknown>): string[] {
  const skipped = summary.skipped_uploads;
  if (!Array.isArray(skipped)) return [];
  return skipped.map((item) => (typeof item === "string" ? item : JSON.stringify(item)));
}
