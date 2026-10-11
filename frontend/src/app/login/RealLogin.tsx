"use client";

import { useEffect, useState } from "react";

import { Notice } from "@/components/ui";
import { errorMessage, passwordLogin, requestLoginCode, verifyLoginCode } from "@/lib/api";
import type { LoginMethods } from "@/types/api";

type Tab = "password" | "code";

const FIELD = "mt-1 block w-full rounded-md border border-rule bg-sheet-2 px-3 py-2 font-normal text-ink placeholder:text-ink-3";
const BUTTON = "rounded-md bg-ink px-4 py-2 font-medium text-sheet hover:opacity-90 disabled:opacity-50";

/** Email + password, or a one-time code sent to the phone: whichever the server offers. */
export function RealLogin({ methods, onDone }: { methods: LoginMethods; onDone: () => void }) {
  const tabs: Tab[] = [...(methods.password ? (["password"] as const) : []), ...(methods.otp ? (["code"] as const) : [])];
  const [tab, setTab] = useState<Tab>(tabs[0] ?? "password");
  if (tabs.length === 0) return null;

  return (
    <section aria-labelledby="real-login" className="mt-8 rounded-lg border border-rule bg-sheet">
      <h2 id="real-login" className="sr-only">
        Log in to your account
      </h2>
      {tabs.length > 1 ? (
        <div role="tablist" aria-label="How to log in" className="flex border-b border-rule">
          {tabs.map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`flex-1 px-4 py-2.5 text-sm font-medium ${tab === t ? "border-b-2 border-khata text-ink" : "text-ink-2 hover:text-ink"}`}
            >
              {t === "password" ? "Email and password" : "Code on my phone"}
            </button>
          ))}
        </div>
      ) : null}
      <div className="px-4 py-5 sm:px-6">
        {tab === "password" ? <PasswordForm onDone={onDone} /> : <CodeForm channel={methods.otp_channel} onDone={onDone} />}
      </div>
    </section>
  );
}

function PasswordForm({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await passwordLogin(email.trim(), password);
      onDone();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      {error ? <Notice title="Could not log in">{error}</Notice> : null}
      <label className="block text-sm font-medium">
        Email
        <input type="email" required autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} className={FIELD} />
      </label>
      <label className="block text-sm font-medium">
        Password
        <input
          type="password"
          required
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className={FIELD}
        />
      </label>
      <button type="submit" disabled={busy || !email.trim() || !password} className={BUTTON}>
        {busy ? "Logging in…" : "Log in"}
      </button>
    </form>
  );
}

function CodeForm({ channel, onDone }: { channel: LoginMethods["otp_channel"]; onDone: () => void }) {
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<string | null>(null); // the server's answer, once asked
  const [wait, setWait] = useState(0); // seconds before another code can be asked for
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (wait <= 0) return;
    const timer = window.setTimeout(() => setWait((w) => w - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [wait]);

  async function ask(event?: React.FormEvent) {
    event?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const answer = await requestLoginCode(phone.trim());
      setSent(answer.message);
      setWait(answer.resend_after);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function verify(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await verifyLoginCode(phone.trim(), code.trim());
      onDone();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {error ? <Notice title="Could not log in">{error}</Notice> : null}
      <form onSubmit={ask} className="space-y-4">
        <label className="block text-sm font-medium">
          Phone number
          <input
            type="tel"
            required
            autoComplete="tel"
            inputMode="tel"
            placeholder="+91 98765 43210"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            disabled={sent !== null}
            className={FIELD}
          />
        </label>
        {sent === null ? (
          <button type="submit" disabled={busy || phone.trim().length < 6} className={BUTTON}>
            {busy ? "Sending…" : channel === "whatsapp" ? "Send me a code on WhatsApp" : "Send me a code"}
          </button>
        ) : null}
      </form>
      {sent !== null ? (
        <form onSubmit={verify} className="space-y-4">
          <Notice tone="info">{sent}</Notice>
          <label className="block text-sm font-medium">
            Six-digit code
            <input
              required
              autoComplete="one-time-code"
              inputMode="numeric"
              pattern="[0-9]{6}"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              className={`${FIELD} tracking-[0.3em]`}
            />
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <button type="submit" disabled={busy || code.length !== 6} className={BUTTON}>
              {busy ? "Checking…" : "Log in"}
            </button>
            <button
              type="button"
              disabled={busy || wait > 0}
              onClick={() => ask()}
              className="text-sm font-medium text-khata disabled:text-ink-3"
            >
              {wait > 0 ? `Send a new code in ${wait} s` : "Send a new code"}
            </button>
            <button
              type="button"
              onClick={() => {
                setSent(null);
                setCode("");
              }}
              className="text-sm text-ink-2 hover:text-ink"
            >
              Use another number
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}
