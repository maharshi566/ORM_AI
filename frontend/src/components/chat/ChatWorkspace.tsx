"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApprovalSlip } from "@/components/ApprovalSlip";
import { RequestPanel } from "@/components/chat/RequestPanel";
import { Markdown } from "@/components/Markdown";
import { RequireLogin } from "@/components/RequireLogin";
import { Notice, Spinner, StatusBadge } from "@/components/ui";
import { chatStream, errorMessage, getSession, getWorkflow, listSessions, listShops } from "@/lib/api";
import { ago, STAGES, statusLabel, toolLabel, when } from "@/lib/format";
import type { Login } from "@/lib/login";
import {
  applyStageEvent,
  type Decision,
  newTurn,
  pauseRail,
  railFromReply,
  stopRail,
  type Turn,
  turnsFromSession,
  withDecision,
  withReply,
  withWorkflow,
} from "@/lib/turns";
import type { ChatResponse, SessionListItem, ShopView } from "@/types/api";

const MAX_LENGTH = 2000;

// Questions about edge cases planted in each demo shop's records (backend/data/seed).
const SUGGESTIONS: Record<string, string[]> = {
  "SHOP-001": [
    "Which products are running low, and should I reorder any of them?",
    "How much does CUST-0001 owe? Can I give them more on credit today?",
    "The customer brought back SALE-005598 today. Please process the return.",
    "Please change the selling price of PRD-0002 to Rs 125.",
  ],
  "SHOP-002": [
    "Purchase order PO-00585 still has not arrived. What should I do?",
    "PO-00585 is 7 days late. Please message the supplier about it.",
  ],
  "SHOP-003": ["PO-00586 arrived with only 20 reams of A4 paper instead of 30. What now?"],
  "SHOP-004": ["The margin on PRD-0085 looks wrong. Is it priced correctly?"],
  "SHOP-005": ["What were my total sales last week?", "A customer wants to return bill SALE-005668. Is that allowed?"],
};
const GENERAL = [
  "Which products are running low, and should I reorder any of them?",
  "What were my total sales last week?",
  "What is the credit limit for a household customer?",
];

async function loadConversation(id: string, signal: AbortSignal): Promise<{ turns: Turn[]; shop: string }> {
  const session = await getSession(id, signal);
  const turns = turnsFromSession(session);
  const records = await Promise.all(
    turns.map((turn) => (turn.workflowId ? getWorkflow(turn.workflowId, signal).catch(() => null) : null)),
  );
  return { turns: turns.map((turn, i) => (records[i] ? withWorkflow(turn, records[i]) : turn)), shop: session.shop_id };
}

export function ChatWorkspace() {
  return <RequireLogin>{(login) => <Workspace login={login} />}</RequireLogin>;
}

function Workspace({ login }: { login: Login }) {
  const router = useRouter();
  const sessionParam = useSearchParams().get("session");

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(Boolean(sessionParam));
  const [loadError, setLoadError] = useState<string | null>(null);
  const [shop, setShop] = useState<string>(login.shopId ?? "SHOP-001");
  const [shops, setShops] = useState<ShopView[]>([]);
  const [sessions, setSessions] = useState<SessionListItem[] | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [sending, setSending] = useState(false);
  const [highlight, setHighlight] = useState<{ index: number; tick: number } | null>(null);
  const generation = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Following the address bar: a different ?session= loads that conversation, none starts afresh.
  const [shownParam, setShownParam] = useState(sessionParam);
  if (sessionParam !== shownParam) {
    setShownParam(sessionParam);
    if (!sessionParam) {
      generation.current += 1;
      setSessionId(null);
      setTurns([]);
      setSelected(null);
      setLoading(false);
      setLoadError(null);
    } else if (sessionParam !== sessionId) {
      setLoading(true);
      setLoadError(null);
    }
  }

  useEffect(() => {
    if (!sessionParam || sessionParam === sessionId) return;
    const controller = new AbortController();
    generation.current += 1;
    loadConversation(sessionParam, controller.signal).then(
      (loaded) => {
        setSessionId(sessionParam);
        setTurns(loaded.turns);
        setSelected(loaded.turns.at(-1)?.key ?? null);
        setShop(loaded.shop);
        setLoading(false);
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setLoadError(errorMessage(error));
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [sessionParam, sessionId]);

  useEffect(() => {
    const controller = new AbortController();
    listSessions({ limit: 30 }, controller.signal).then(
      (result) => setSessions(result.sessions),
      () => undefined,
    );
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (login.role !== "admin") return;
    const controller = new AbortController();
    listShops(controller.signal).then(
      (result) => setShops(result.shops),
      () => undefined,
    );
    return () => controller.abort();
  }, [login.role]);

  useEffect(() => {
    const box = scrollRef.current;
    if (box) box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
  }, [turns.length, sending]);

  useEffect(() => {
    const onChange = () => setRefresh((n) => n + 1);
    window.addEventListener("orm-ai:approvals-changed", onChange);
    return () => window.removeEventListener("orm-ai:approvals-changed", onChange);
  }, []);

  const updateTurn = useCallback((key: string, change: (turn: Turn) => Turn) => {
    setTurns((all) => all.map((turn) => (turn.key === key ? change(turn) : turn)));
  }, []);

  async function ask(text: string) {
    const question = text.trim();
    if (!question || sending) return;
    const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const mine = generation.current;
    setTurns((all) => [...all, newTurn(key, question, new Date().toISOString())]);
    setSelected(key);
    setSending(true);
    try {
      const reply = await chatStream(
        { shop_id: shop, message: question, session_id: sessionId },
        {
          onStage: (event) => {
            if (mine === generation.current) updateTurn(key, (t) => (t.live ? { ...t, live: applyStageEvent(t.live, event) } : t));
          },
          onApproval: () => {
            if (mine === generation.current) updateTurn(key, (t) => (t.live ? { ...t, live: pauseRail(t.live) } : t));
          },
        },
      );
      if (mine !== generation.current) return;
      updateTurn(key, (t) => withReply(t, reply));
      if (!sessionId) {
        setSessionId(reply.session_id);
        setShownParam(reply.session_id);
        router.replace(`/chat?session=${encodeURIComponent(reply.session_id)}`, { scroll: false });
      }
      if (reply.status === "awaiting_approval") window.dispatchEvent(new Event("orm-ai:approvals-changed"));
    } catch (error) {
      if (mine === generation.current) {
        updateTurn(key, (t) => ({ ...t, error: errorMessage(error), live: t.live ? stopRail(t.live) : null }));
      }
    } finally {
      setSending(false);
      setRefresh((n) => n + 1);
    }
  }

  function startNew() {
    if (sessionParam) {
      router.push("/chat");
    } else {
      generation.current += 1;
      setSessionId(null);
      setTurns([]);
      setSelected(null);
    }
  }

  function cite(turnKey: string, index: number) {
    setSelected(turnKey);
    setHighlight((h) => ({ index, tick: (h?.tick ?? 0) + 1 }));
  }

  const current = turns.find((t) => t.key === selected) ?? turns.at(-1) ?? null;
  const rail = current?.live ?? (current?.reply ? railFromReply(current.reply) : null);
  const shopName = login.role === "admin" ? shops.find((s) => s.shop_id === shop)?.name : login.shopName;
  const suggestions = SUGGESTIONS[shop] ?? GENERAL;

  return (
    <div className="mx-auto grid w-full max-w-[1440px] flex-1 grid-cols-1 lg:h-[calc(100dvh-4rem)] lg:grid-cols-[15rem_minmax(0,1fr)_22rem] lg:overflow-hidden">
      <aside aria-label="Conversations" className="border-b border-rule bg-paper lg:overflow-y-auto lg:border-b-0 lg:border-r">
        <ConversationList sessions={sessions} activeId={sessionId} onNew={startNew} />
      </aside>

      <section aria-label="Conversation" className="flex min-h-[70dvh] flex-col bg-sheet lg:min-h-0">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-rule px-5 py-3">
          <div className="min-w-0">
            <p className="truncate font-serif text-[15px] font-bold">{shopName ?? shop}</p>
            <p className="text-xs text-ink-3">
              {turns.length ? `${turns.length} ${turns.length === 1 ? "request" : "requests"} in this conversation` : "New conversation"}
            </p>
          </div>
          {login.role === "admin" ? (
            <label className="flex items-center gap-2 text-sm text-ink-2">
              Shop
              <select
                value={shop}
                disabled={turns.length > 0}
                onChange={(e) => setShop(e.target.value)}
                className="max-w-56 rounded-md border border-rule bg-sheet px-2 py-1 text-ink disabled:opacity-60"
              >
                {(shops.length ? shops : [{ shop_id: shop, name: shop }]).map((s) => (
                  <option key={s.shop_id} value={s.shop_id}>
                    {s.shop_id} {s.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>

        <div ref={scrollRef} className="ledger-margin flex-1 overflow-y-auto py-6 lg:min-h-0">
          {loading ? (
            <div className="pl-[2.6rem] sm:pl-[4.5rem]">
              <Spinner label="Opening the conversation" />
            </div>
          ) : null}
          {loadError ? (
            <div className="pl-[2.6rem] pr-4 sm:pl-[4.5rem] sm:pr-5">
              <Notice title="Could not open this conversation" action={<button type="button" className="font-medium underline" onClick={startNew}>Start a new one</button>}>
                {loadError}
              </Notice>
            </div>
          ) : null}
          {!loading && !loadError && turns.length === 0 ? (
            <EmptyState suggestions={suggestions} onPick={ask} disabled={sending} />
          ) : null}
          <ol className="space-y-8">
            {turns.map((turn, index) => (
              <TurnEntry
                key={turn.key}
                number={index + 1}
                turn={turn}
                login={login}
                selected={current?.key === turn.key}
                onSelect={() => setSelected(turn.key)}
                onCite={(i) => cite(turn.key, i)}
                onRetry={() => ask(turn.question)}
                onDecided={(reply, decision) => updateTurn(turn.key, (t) => withDecision(t, reply, decision))}
              />
            ))}
          </ol>
        </div>

        <Composer disabled={sending || loading} onSend={ask} />
      </section>

      <aside aria-label="Request details" className="border-t border-rule bg-paper lg:overflow-y-auto lg:border-l lg:border-t-0">
        <RequestPanel
          rail={rail}
          live={Boolean(current?.live && !current.reply && !current.error)}
          reply={current?.reply ?? null}
          highlight={highlight ? highlight.index : null}
          tick={highlight?.tick ?? 0}
        />
      </aside>
    </div>
  );
}

function ConversationList({
  sessions,
  activeId,
  onNew,
}: {
  sessions: SessionListItem[] | null;
  activeId: string | null;
  onNew: () => void;
}) {
  return (
    <div className="p-3">
      <button
        type="button"
        onClick={onNew}
        className="w-full rounded-md border border-ink px-3 py-2 text-sm font-semibold hover:bg-sheet"
      >
        New conversation
      </button>
      <h2 className="mt-5 px-1 font-serif text-sm font-bold">Your conversations</h2>
      {sessions === null ? <p className="mt-2 px-1 text-sm text-ink-3">Loading…</p> : null}
      {sessions?.length === 0 ? <p className="mt-2 px-1 text-sm text-ink-3">None yet. Ask your first question.</p> : null}
      <ul className="mt-2 max-h-56 space-y-0.5 overflow-y-auto lg:max-h-none">
        {sessions?.map((s) => (
          <li key={s.session_id}>
            <Link
              href={`/chat?session=${encodeURIComponent(s.session_id)}`}
              aria-current={s.session_id === activeId ? "page" : undefined}
              className={`block rounded-md px-2 py-1.5 text-sm ${
                s.session_id === activeId ? "bg-sheet shadow-[inset_3px_0_0_var(--khata)]" : "hover:bg-sheet"
              }`}
            >
              <span className="line-clamp-2 leading-snug">{s.title ?? "Untitled"}</span>
              <span className="mt-0.5 flex items-center gap-2 text-xs text-ink-3">
                {ago(s.last_active_at)}
                {s.last_status === "awaiting_approval" ? <span className="font-semibold text-khata">waiting for approval</span> : null}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

function EmptyState({ suggestions, onPick, disabled }: { suggestions: string[]; onPick: (text: string) => void; disabled: boolean }) {
  return (
    <div className="pl-[2.6rem] pr-4 sm:pl-[4.5rem] sm:pr-5">
      <h1 className="font-serif text-xl font-extrabold">What do you want to know about the shop?</h1>
      <p className="mt-2 max-w-[60ch] text-ink-2">
        Ask about stock, sales, supplier orders or customer credit. The agents look up your records and the shop&apos;s
        rules, and nothing that moves money or stock happens until a person approves it.
      </p>
      <ul className="mt-5 space-y-2">
        {suggestions.map((text) => (
          <li key={text}>
            <button
              type="button"
              disabled={disabled}
              onClick={() => onPick(text)}
              className="w-full max-w-[60ch] rounded-md border border-rule bg-sheet-2 px-3 py-2 text-left text-[15px] hover:border-rule-strong disabled:opacity-60"
            >
              {text}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TurnEntry({
  number,
  turn,
  login,
  selected,
  onSelect,
  onCite,
  onRetry,
  onDecided,
}: {
  number: number;
  turn: Turn;
  login: Login;
  selected: boolean;
  onSelect: () => void;
  onCite: (index: number) => void;
  onRetry: () => void;
  onDecided: (reply: ChatResponse, decision: Decision) => void;
}) {
  const reply = turn.reply;
  const sources = reply?.sources ?? [];
  const working = turn.live && !reply && !turn.error;
  const activeStage = working ? STAGES.find((s) => turn.live?.stages[s.id].state === "active") : undefined;
  const waiting = Boolean(turn.request && !turn.decision && reply?.status === "awaiting_approval");

  return (
    <li className="grid grid-cols-[2.6rem_minmax(0,1fr)] pr-4 sm:grid-cols-[4.5rem_minmax(0,1fr)] sm:pr-5">
      <div className="pr-[1.35rem] pt-0.5 text-right font-serif text-sm font-bold text-ink-3 sm:pr-7" aria-hidden>
        {number}
      </div>
      <article aria-label={`Request ${number}`} className="min-w-0">
        <header className="flex flex-wrap items-baseline justify-between gap-x-3">
          <p className="max-w-[65ch] text-[17px] font-semibold leading-snug">{turn.question}</p>
          <time className="text-xs text-ink-3" dateTime={turn.at}>
            {when(turn.at)}
          </time>
        </header>

        <div className="mt-3 max-w-[70ch]">
          {working ? <Spinner label={activeStage ? `${activeStage.label}…` : "Starting the agents…"} /> : null}

          {turn.error ? (
            <Notice
              title="This request did not finish"
              action={
                <button type="button" onClick={onRetry} className="font-medium underline underline-offset-2">
                  Ask again
                </button>
              }
            >
              {turn.error}
            </Notice>
          ) : null}

          {turn.decision && !turn.request ? <DecidedLine decision={turn.decision} login={login} /> : null}

          {waiting ? (
            <p className="text-ink-2">Nothing has been changed yet. ORM_AI needs a decision first.</p>
          ) : turn.answer && !(turn.decision && turn.request) ? (
            <Markdown text={turn.answer} sources={sources} onCite={onCite} />
          ) : null}

          {turn.request && (waiting || turn.decision) ? (
            <ApprovalSlip
              request={turn.request}
              login={login}
              sources={sources}
              onCite={onCite}
              decision={turn.decision}
              results={turn.decision ? (reply?.proposed_actions ?? []) : []}
              onDecided={onDecided}
            />
          ) : null}

          {turn.decision && turn.request && turn.answer ? (
            <div className="mt-5">
              <p className="mb-2 font-serif text-sm font-bold text-ink-2">After the decision</p>
              <Markdown text={turn.answer} sources={sources} onCite={onCite} />
            </div>
          ) : null}

          {reply ? (
            <footer className="mt-3 flex flex-wrap items-center gap-3 text-sm">
              <StatusBadge status={reply.status} />
              {reply.warnings.length ? <span className="text-xs text-ink-3">{reply.warnings.length} note(s) in the record</span> : null}
              <button
                type="button"
                onClick={onSelect}
                aria-pressed={selected}
                className={`text-xs font-medium underline-offset-2 hover:underline ${selected ? "text-khata" : "text-ink-2"}`}
              >
                {selected ? (
                  <>
                    <span className="lg:hidden">Shown below</span>
                    <span className="hidden lg:inline">Shown on the right</span>
                  </>
                ) : (
                  "Show how this was worked out"
                )}
              </button>
            </footer>
          ) : null}
        </div>
      </article>
    </li>
  );
}

function DecidedLine({ decision, login }: { decision: Decision; login: Login }) {
  const who = decision.by === login.userId ? "you" : (decision.by ?? "someone");
  const what = decision.tools?.length ? decision.tools.map(toolLabel).join(", ") : "The proposed action";
  return (
    <p className="mb-3 rounded-md border border-rule bg-sheet-2 px-3 py-2 text-sm">
      <span className="font-semibold">{what}</span>: {statusLabel(decision.status).label.toLowerCase()} by {who}
      {decision.at ? `, ${when(decision.at)}` : ""}
      {decision.note ? <span className="block text-ink-2">Note: {decision.note}</span> : null}
    </p>
  );
}

function Composer({ disabled, onSend }: { disabled: boolean; onSend: (text: string) => void }) {
  const [text, setText] = useState("");
  const tooLong = text.length > MAX_LENGTH;
  const rows = useMemo(() => Math.min(6, Math.max(2, text.split("\n").length)), [text]);

  function submit() {
    if (disabled || tooLong || !text.trim()) return;
    onSend(text);
    setText("");
  }

  return (
    <form
      className="border-t border-rule bg-sheet px-5 py-3"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <label htmlFor="question" className="sr-only">
        Your question
      </label>
      <div className="flex items-end gap-3">
        <textarea
          id="question"
          value={text}
          rows={rows}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="Ask about stock, sales, suppliers or customer credit"
          className="min-w-0 flex-1 resize-none rounded-md border border-rule-strong bg-sheet px-3 py-2 text-[16px] placeholder:text-ink-3"
        />
        <button
          type="submit"
          disabled={disabled || tooLong || !text.trim()}
          className="rounded-md bg-khata px-5 py-2.5 font-semibold text-sheet hover:opacity-90 disabled:opacity-40"
        >
          {disabled ? "Working…" : "Ask"}
        </button>
      </div>
      <p className={`mt-1.5 text-xs ${tooLong ? "font-semibold text-khata" : "text-ink-3"}`}>
        {tooLong ? `${text.length - MAX_LENGTH} characters too long.` : "Enter sends, Shift+Enter starts a new line."}
      </p>
    </form>
  );
}
