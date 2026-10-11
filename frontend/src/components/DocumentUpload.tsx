"use client";

import { useEffect, useState } from "react";

import { Notice, Spinner, StatusBadge } from "@/components/ui";
import { errorMessage, getIngestJob, listShops, startIngestion, uploadDocument } from "@/lib/api";
import { count } from "@/lib/format";
import type { Login } from "@/lib/login";
import { CATEGORY_LABEL, ingestSummary, skippedUploads, UPLOAD_ACCEPT, uploadProblem } from "@/lib/uploads";
import type { DocumentCategory, IngestJobResponse, ShopView, UploadResponse } from "@/types/api";

const INPUT = "mt-1 block w-full rounded-md border border-rule bg-sheet px-3 py-2 text-ink";
const POLL_MS = 2000;

/** Follows one ingestion job until it is done; keyed by job, so a new job starts fresh. */
function IngestProgress({ jobId, onFinished }: { jobId: string; onFinished: () => void }) {
  const [job, setJob] = useState<IngestJobResponse | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    const poll = () =>
      getIngestJob(jobId, controller.signal).then(
        (next) => {
          setJob(next);
          if (next.status === "running") timer = window.setTimeout(poll, POLL_MS);
          else onFinished();
        },
        (error: unknown) => {
          if (!controller.signal.aborted) setProblem(errorMessage(error));
        },
      );
    poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one job, followed once; onFinished only reports back
  }, [jobId]);

  if (problem) return <Notice title="Could not follow the ingestion">{problem}</Notice>;
  if (!job || job.status === "running") return <Spinner label="Reading the documents into the knowledge base" />;
  if (job.status === "failed") return <Notice title="Ingestion failed">{job.error ?? "The server gave no reason."}</Notice>;

  const skipped = skippedUploads(job.summary);
  return (
    <Notice tone="done" title="The knowledge base is up to date">
      <dl className="mt-1 flex flex-wrap gap-x-6 gap-y-1">
        {ingestSummary(job.summary).map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-ink-3">{label}</dt>
            <dd className="font-semibold tabular-nums text-ink">{count(value)}</dd>
          </div>
        ))}
      </dl>
      {skipped.length ? <p className="mt-2 text-amber">Not used (could not be read): {skipped.join("; ")}</p> : null}
    </Notice>
  );
}

/**
 * Add a document the agents can search and cite, then ingest it. Owners may mark it as a
 * shop rule the agents rely on; for anyone else it is kept as reference only (the backend
 * enforces this too). Admins choose the shop, or every shop.
 */
export function DocumentUpload({ login, onIngested }: { login: Login; onIngested?: () => void }) {
  const canTrust = login.role === "owner" || login.role === "admin";
  const [file, setFile] = useState<File | null>(null);
  const [fileKey, setFileKey] = useState(0);
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<DocumentCategory>("sop");
  const [trusted, setTrusted] = useState(false);
  const [shopId, setShopId] = useState("");
  const [shops, setShops] = useState<ShopView[]>([]);
  const [busy, setBusy] = useState<"upload" | "ingest" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [uploaded, setUploaded] = useState<UploadResponse | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  useEffect(() => {
    if (login.role !== "admin") return;
    const controller = new AbortController();
    listShops(controller.signal).then(
      (result) => setShops(result.shops),
      () => undefined, // the list is a convenience; "every shop" still works
    );
    return () => controller.abort();
  }, [login.role]);

  const fileProblem = file ? uploadProblem(file.name, file.size) : null;

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    if (!file || fileProblem) return;
    const form = new FormData();
    form.append("file", file);
    if (title.trim()) form.append("title", title.trim());
    form.append("category", category);
    form.append("trusted", String(canTrust && trusted));
    form.append("ingest", "true");
    if (login.role === "admin" && shopId) form.append("shop_id", shopId);
    setBusy("upload");
    setProblem(null);
    try {
      const reply = await uploadDocument(form);
      setUploaded(reply);
      setJobId(reply.ingest_job_id);
      setFile(null);
      setTitle("");
      setFileKey((key) => key + 1); // clears the file picker
    } catch (error) {
      setProblem(errorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  async function ingestAll() {
    setBusy("ingest");
    setProblem(null);
    try {
      setJobId((await startIngestion()).job_id);
    } catch (error) {
      setProblem(errorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[3fr_2fr]">
      <form onSubmit={upload} className="space-y-4 rounded-lg border border-rule bg-sheet p-5">
        <label className="block text-sm">
          <span className="font-medium">File</span>
          <span className="text-ink-3"> (.md, .txt or .pdf, up to 5 MB)</span>
          <input
            key={fileKey}
            type="file"
            accept={UPLOAD_ACCEPT}
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            className="mt-1 block w-full text-sm text-ink-2 file:mr-3 file:rounded-md file:border file:border-rule file:bg-sheet-2 file:px-3 file:py-1.5 file:font-medium file:text-ink hover:file:border-rule-strong"
          />
          {fileProblem ? <span className="mt-1 block text-amber">{fileProblem}</span> : null}
        </label>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block text-sm">
            <span className="font-medium">Title</span>
            <span className="text-ink-3"> (shown when it is cited)</span>
            <input value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} placeholder={file?.name ?? "Diwali opening hours"} className={INPUT} />
          </label>
          <label className="block text-sm">
            <span className="font-medium">Kind of document</span>
            <select value={category} onChange={(event) => setCategory(event.target.value as DocumentCategory)} className={INPUT}>
              {(Object.keys(CATEGORY_LABEL) as DocumentCategory[]).map((value) => (
                <option key={value} value={value}>
                  {CATEGORY_LABEL[value]}
                </option>
              ))}
            </select>
          </label>
        </div>

        {login.role === "admin" ? (
          <label className="block text-sm">
            <span className="font-medium">For</span>
            <select value={shopId} onChange={(event) => setShopId(event.target.value)} className={`${INPUT} sm:w-80`}>
              <option value="">Every shop</option>
              {shops.map((shop) => (
                <option key={shop.shop_id} value={shop.shop_id}>
                  {shop.name} ({shop.shop_id})
                </option>
              ))}
            </select>
          </label>
        ) : (
          <p className="text-sm text-ink-2">Kept for {login.shopName ?? login.shopId}.</p>
        )}

        <label className="flex items-start gap-2.5 text-sm">
          <input type="checkbox" checked={canTrust && trusted} disabled={!canTrust} onChange={(event) => setTrusted(event.target.checked)} className="mt-1" />
          <span>
            <span className="font-medium">The agents may rely on it like a shop rule</span>
            <span className="block text-ink-2">
              {canTrust
                ? "Leave this off for reference material such as a supplier's flyer: the agents never act on that alone."
                : "Only an owner can make a document a shop rule; yours is kept as reference only."}
            </span>
          </span>
        </label>

        <div className="flex flex-wrap items-center justify-end gap-3">
          {canTrust ? (
            <button
              type="button"
              onClick={ingestAll}
              disabled={busy !== null}
              className="rounded-md border border-rule px-3 py-2 text-sm font-medium text-ink-2 hover:border-rule-strong hover:text-ink disabled:opacity-50"
            >
              {busy === "ingest" ? "Starting…" : "Re-read everything"}
            </button>
          ) : null}
          <button
            type="submit"
            disabled={!file || Boolean(fileProblem) || busy !== null}
            className="rounded-md bg-ink px-4 py-2 font-medium text-sheet hover:opacity-90 disabled:opacity-50"
          >
            {busy === "upload" ? "Uploading…" : "Upload and read in"}
          </button>
        </div>
      </form>

      <div className="space-y-3" aria-live="polite">
        {problem ? <Notice title="That did not work">{problem}</Notice> : null}
        {uploaded ? (
          <div className="rounded-lg border border-rule bg-sheet p-4 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold">{uploaded.title}</span>
              <StatusBadge status={uploaded.trust === "trusted" ? "completed" : null} label={uploaded.trust === "trusted" ? "Shop rule" : "Reference only"} />
            </div>
            <p className="mt-1 text-ink-2">
              {CATEGORY_LABEL[uploaded.category]} · {uploaded.shop_id ?? "every shop"} · {count(Math.max(1, Math.round(uploaded.size_bytes / 1024)))} KB
            </p>
            <p className="text-xs tabular-nums text-ink-3">{uploaded.document_id}</p>
            {uploaded.already_uploaded ? <p className="mt-2 text-ink-2">This file was already uploaded; nothing changed.</p> : null}
            {uploaded.notes.map((note) => (
              <p key={note} className="mt-2 text-amber">
                {note}
              </p>
            ))}
          </div>
        ) : null}
        {jobId ? <IngestProgress key={jobId} jobId={jobId} onFinished={() => onIngested?.()} /> : null}
        {!uploaded && !jobId && !problem ? (
          <p className="text-sm text-ink-2">
            Uploaded documents are searched like the shop&apos;s own rules and cited in answers. Try asking about one in the
            chat once it has been read in.
          </p>
        ) : null}
      </div>
    </div>
  );
}
