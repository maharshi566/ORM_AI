"use client";

import { useCallback, useEffect, useState } from "react";

import { RequireLogin } from "@/components/RequireLogin";
import { Notice, PageTitle, SectionTitle, Spinner } from "@/components/ui";
import { changePassword, createUser, errorMessage, listUsers, updateUser } from "@/lib/api";
import { ago } from "@/lib/format";
import { type Login, ROLE_LABEL } from "@/lib/login";
import type { TeamUser } from "@/types/api";

const FIELD = "mt-1 block w-full rounded-md border border-rule bg-sheet-2 px-3 py-2 font-normal text-ink placeholder:text-ink-3";
const BUTTON = "rounded-md bg-ink px-4 py-2 font-medium text-sheet hover:opacity-90 disabled:opacity-50";

export function AccountPage() {
  return <RequireLogin>{(login) => <Account login={login} />}</RequireLogin>;
}

function Account({ login }: { login: Login }) {
  const manages = login.role === "owner" || login.role === "admin";
  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-10 sm:px-6">
      <PageTitle title="Account">
        {login.name ?? login.userId} · {ROLE_LABEL[login.role]} · {login.shopName ?? "Every shop"}
      </PageTitle>
      <PasswordSection />
      {manages ? <TeamSection login={login} /> : null}
    </div>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (next !== again) {
      setResult({ ok: false, text: "The two new passwords differ." });
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      await changePassword(current, next);
      setCurrent("");
      setNext("");
      setAgain("");
      setResult({ ok: true, text: "Password changed. Your other devices are logged out." });
    } catch (err) {
      setResult({ ok: false, text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-10" aria-labelledby="password">
      <SectionTitle title="Password">At least 10 characters; a short sentence works well.</SectionTitle>
      <form onSubmit={submit} className="grid gap-4 rounded-lg border border-rule bg-sheet px-4 py-5 sm:grid-cols-3 sm:px-6">
        <label className="block text-sm font-medium">
          Current password
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            placeholder="Empty if you never had one"
            className={FIELD}
          />
        </label>
        <label className="block text-sm font-medium">
          New password
          <input type="password" required autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} className={FIELD} />
        </label>
        <label className="block text-sm font-medium">
          New password again
          <input type="password" required autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} className={FIELD} />
        </label>
        <div className="sm:col-span-3">
          {result ? (
            <div className="mb-3">
              <Notice tone={result.ok ? "done" : "problem"}>{result.text}</Notice>
            </div>
          ) : null}
          <button type="submit" disabled={busy || !next} className={BUTTON}>
            {busy ? "Saving…" : "Change password"}
          </button>
        </div>
      </form>
    </section>
  );
}

type TeamState = { kind: "loading" } | { kind: "ready"; users: TeamUser[] } | { kind: "error"; message: string };

function TeamSection({ login }: { login: Login }) {
  const [team, setTeam] = useState<TeamState>({ kind: "loading" });
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback((signal?: AbortSignal) => {
    listUsers(signal).then(
      (users) => setTeam({ kind: "ready", users }),
      (err: unknown) => {
        if (!signal?.aborted) setTeam({ kind: "error", message: errorMessage(err) });
      },
    );
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function toggle(user: TeamUser) {
    setBusy(user.user_id);
    setError(null);
    try {
      await updateUser(user.user_id, { is_active: !user.is_active });
      load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="mt-12" aria-labelledby="team">
      <SectionTitle title={login.role === "admin" ? "Users" : "Your team"}>
        {login.role === "admin" ? "Every shop's users." : "Staff can approve small refunds and reminders; you approve the rest."}
      </SectionTitle>
      {error ? (
        <div className="mb-3">
          <Notice title="Not changed">{error}</Notice>
        </div>
      ) : null}
      {team.kind === "loading" ? <Spinner label="Loading the team" /> : null}
      {team.kind === "error" ? <Notice>{team.message}</Notice> : null}
      {team.kind === "ready" ? (
        <div className="overflow-x-auto rounded-lg border border-rule bg-sheet">
          <table className="w-full text-sm">
            <thead className="bg-sheet-2 text-left text-ink-2">
              <tr>
                <th className="px-4 py-2 font-medium">Name</th>
                <th className="px-4 py-2 font-medium">Logs in with</th>
                <th className="px-4 py-2 font-medium">Last login</th>
                <th className="px-4 py-2 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-rule">
              {team.users.map((user) => (
                <tr key={user.user_id} className={user.is_active ? "" : "text-ink-3"}>
                  <td className="px-4 py-2">
                    <span className="font-medium">{user.name}</span> <span className="text-ink-3">({ROLE_LABEL[user.role].toLowerCase()})</span>
                    <span className="block text-xs text-ink-3">
                      {user.user_id}
                      {login.role === "admin" && user.shop_id ? ` · ${user.shop_id}` : ""}
                    </span>
                  </td>
                  <td className="px-4 py-2">
                    {[user.has_password && user.email ? user.email : null, user.phone ? `${user.phone} (code)` : null]
                      .filter(Boolean)
                      .join(" · ") || "—"}
                  </td>
                  <td className="px-4 py-2">{user.last_login_at ? ago(user.last_login_at) : "Never"}</td>
                  <td className="px-4 py-2 text-right">
                    {user.user_id !== login.userId && (login.role === "admin" || user.role === "staff") ? (
                      <button
                        type="button"
                        disabled={busy !== null}
                        onClick={() => toggle(user)}
                        className="rounded-md border border-rule px-2.5 py-1 text-ink-2 hover:border-rule-strong hover:text-ink disabled:opacity-50"
                      >
                        {busy === user.user_id ? "Saving…" : user.is_active ? "Switch off" : "Switch on"}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {login.role === "owner" ? <AddStaff onAdded={() => load()} /> : null}
    </section>
  );
}

function AddStaff({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const user = await createUser({
        name: name.trim(),
        role: "staff",
        email: email.trim() || null,
        phone: phone.trim() || null,
        password: password || null,
      });
      setName("");
      setEmail("");
      setPhone("");
      setPassword("");
      setResult({ ok: true, text: `${user.name} can log in now (${user.user_id}).` });
      onAdded();
    } catch (err) {
      setResult({ ok: false, text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="mt-6 rounded-lg border border-rule bg-sheet px-4 py-5 sm:px-6">
      <h3 className="font-semibold">Add a staff member</h3>
      <p className="mt-1 text-sm text-ink-2">
        Give an email and a starting password, a phone number for login codes, or both. Tell them their password in person.
      </p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <label className="block text-sm font-medium">
          Name
          <input required minLength={2} value={name} onChange={(e) => setName(e.target.value)} className={FIELD} />
        </label>
        <label className="block text-sm font-medium">
          Phone (for login codes)
          <input type="tel" inputMode="tel" placeholder="+91 98765 43210" value={phone} onChange={(e) => setPhone(e.target.value)} className={FIELD} />
        </label>
        <label className="block text-sm font-medium">
          Email
          <input type="email" autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} className={FIELD} />
        </label>
        <label className="block text-sm font-medium">
          Starting password
          <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} className={FIELD} />
        </label>
      </div>
      {result ? (
        <div className="mt-4">
          <Notice tone={result.ok ? "done" : "problem"}>{result.text}</Notice>
        </div>
      ) : null}
      <button type="submit" disabled={busy || name.trim().length < 2 || (!email.trim() && !phone.trim())} className={`${BUTTON} mt-4`}>
        {busy ? "Adding…" : "Add staff member"}
      </button>
    </form>
  );
}
