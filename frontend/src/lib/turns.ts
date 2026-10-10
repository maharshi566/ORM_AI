// The chat page's model: each question is one entry (a "turn") in the conversation,
// and each turn has a rail showing which agents ran. Pure functions only, so they
// are tested on their own (turns.test.ts) with Node's test runner.

import type {
  AgentStep,
  ApprovalRecordView,
  ApprovalRequestView,
  ChatResponse,
  SessionResponse,
  StageEvent,
} from "@/types/api";

/** The workflow stages the chat shows, in the order a request usually goes through them. */
export const STAGES = [
  { id: "triage", label: "Understand the request", agents: ["triage"] },
  { id: "retrieval", label: "Look up records and rules", agents: ["data_retrieval", "knowledge"] },
  { id: "investigation", label: "Investigate", agents: ["investigation"] },
  { id: "approval", label: "Ask a person", agents: ["human_review"] },
  { id: "action", label: "Carry out what was approved", agents: ["action"] },
  { id: "response", label: "Write the reply", agents: ["respond", "clarify", "finalize"] },
  { id: "validation", label: "Check the reply", agents: ["validate"] },
] as const;

export type StageId = (typeof STAGES)[number]["id"];

export function stageOf(agent: string): StageId | null {
  for (const stage of STAGES) {
    if ((stage.agents as readonly string[]).includes(agent)) return stage.id;
  }
  return null;
}

export type StageState = {
  state: "waiting" | "active" | "done" | "failed" | "skipped" | "paused";
  summary: string | null;
  latencyMs: number;
  active: string[]; // agents of this stage running right now
};

export type Rail = { stages: Record<StageId, StageState>; supervisor: string | null };

export function emptyRail(): Rail {
  const stages = {} as Record<StageId, StageState>;
  for (const stage of STAGES) stages[stage.id] = { state: "waiting", summary: null, latencyMs: 0, active: [] };
  return { stages, supervisor: null };
}

/** The rail after one live event from the stream. Returns a new object. */
export function applyStageEvent(rail: Rail, event: StageEvent): Rail {
  if (event.agent === "supervisor") {
    return event.state === "finished" && event.summary ? { ...rail, supervisor: event.summary } : rail;
  }
  const id = stageOf(event.agent);
  if (!id) return rail;
  const current = rail.stages[id];
  let next: StageState;
  if (event.state === "started") {
    next = { ...current, state: "active", active: [...current.active.filter((a) => a !== event.agent), event.agent] };
  } else {
    const active = current.active.filter((a) => a !== event.agent);
    const failed = event.status === "error" || current.state === "failed";
    next = {
      state: active.length ? "active" : failed ? "failed" : "done",
      summary: event.summary ?? current.summary,
      latencyMs: current.latencyMs + (event.latency_ms ?? 0),
      active,
    };
  }
  return { ...rail, stages: { ...rail.stages, [id]: next } };
}

/** The rail once the run has paused for a person. */
export function pauseRail(rail: Rail): Rail {
  return {
    ...rail,
    stages: { ...rail.stages, approval: { ...rail.stages.approval, state: "paused", active: [] } },
  };
}

/** The rail when the run broke off: what was working is marked as failed. */
export function stopRail(rail: Rail): Rail {
  const stages = { ...rail.stages };
  for (const stage of STAGES) {
    if (stages[stage.id].state === "active") stages[stage.id] = { ...stages[stage.id], state: "failed", active: [] };
  }
  return { ...rail, stages };
}

/** The rail of a finished (or paused) reply, from its agent steps. */
export function railFromReply(reply: Pick<ChatResponse, "agents" | "status">): Rail {
  const rail = emptyRail();
  const steps: AgentStep[] = reply.agents ?? [];
  for (const stage of STAGES) {
    const mine = steps.filter((s) => (stage.agents as readonly string[]).includes(s.agent));
    if (!mine.length) {
      rail.stages[stage.id].state = "skipped";
      continue;
    }
    rail.stages[stage.id] = {
      state: mine.some((s) => s.status !== "success") ? "failed" : "done",
      summary: mine[mine.length - 1].summary || null,
      latencyMs: mine.reduce((total, s) => total + (s.latency_ms ?? 0), 0),
      active: [],
    };
  }
  if (reply.status === "awaiting_approval") {
    rail.stages.approval.state = "paused";
    for (const id of ["action", "response", "validation"] as const) {
      if (rail.stages[id].state === "skipped") rail.stages[id].state = "waiting";
    }
  }
  const lastRoute = [...steps].reverse().find((s) => s.agent === "supervisor");
  rail.supervisor = lastRoute?.summary ?? null;
  return rail;
}

// ------------------------------------------------------------------ turns

export type Decision = {
  status: string; // approved, modified, rejected, or mixed
  by: string | null;
  note: string | null;
  at: string | null;
  tools?: string[]; // what was decided (for stored conversations)
};

export type Turn = {
  key: string;
  workflowId: string | null;
  question: string;
  at: string;
  /** The latest reply: the pause while it waits, the final answer once decided. */
  reply: ChatResponse | null;
  /** The answer text (set even when the full reply could not be loaded). */
  answer: string | null;
  /** What a person was asked to decide, when the workflow paused. */
  request: ApprovalRequestView | null;
  /** The message shown while it waited (kept after the decision). */
  pauseAnswer: string | null;
  decision: Decision | null;
  live: Rail | null;
  error: string | null;
};

export function newTurn(key: string, question: string, at: string): Turn {
  return {
    key,
    workflowId: null,
    question,
    at,
    reply: null,
    answer: null,
    request: null,
    pauseAnswer: null,
    decision: null,
    live: emptyRail(),
    error: null,
  };
}

/** A turn after its first reply arrives (from the stream or POST /api/chat). */
export function withReply(turn: Turn, reply: ChatResponse): Turn {
  const paused = reply.status === "awaiting_approval";
  return {
    ...turn,
    workflowId: reply.workflow_id,
    reply,
    answer: reply.answer,
    request: paused ? reply.approval : turn.request,
    pauseAnswer: paused ? reply.answer : turn.pauseAnswer,
    live: null,
    error: null,
  };
}

/** A turn after a person decided and the workflow resumed. */
export function withDecision(turn: Turn, reply: ChatResponse, decision: Decision): Turn {
  return { ...turn, reply, answer: reply.answer, decision, live: null, error: null };
}

export function decisionFrom(records: ApprovalRecordView[]): Decision | null {
  const decided = records.filter((r) => r.status !== "pending");
  if (!decided.length) return null;
  const statuses = new Set(decided.map((r) => r.status));
  const last = decided[decided.length - 1];
  return {
    status: statuses.size === 1 ? decided[0].status : "mixed",
    by: last.decided_by,
    note: last.decision_note,
    at: last.decided_at,
    tools: decided.map((r) => r.tool),
  };
}

const DECISION_MESSAGE = /^\[decision by [^\]]*\]/;

/**
 * Turns from a stored conversation: messages grouped by workflow. The first user
 * message is the question; "[decision by …]" messages mark a decision; the first
 * assistant message after a decision-free start is the pause, the last one the answer.
 */
export function turnsFromSession(session: SessionResponse): Turn[] {
  const turns: Turn[] = [];
  const byWorkflow = new Map<string, Turn>();
  session.messages.forEach((message, index) => {
    const id = message.workflow_id;
    let turn = id ? byWorkflow.get(id) : undefined;
    if (message.role === "user" && DECISION_MESSAGE.test(message.content)) {
      if (turn) turn.pauseAnswer = turn.pauseAnswer ?? turn.answer;
      return;
    }
    if (message.role === "user" && !turn) {
      turn = { ...newTurn(`${session.session_id}-${index}`, message.content, message.created_at), live: null, workflowId: id };
      turns.push(turn);
      if (id) byWorkflow.set(id, turn);
      return;
    }
    if (message.role === "assistant" && turn) turn.answer = message.content;
  });
  return turns;
}

/** Fills a stored turn with what the workflow record says (reply, request, decision). */
export function withWorkflow(
  turn: Turn,
  workflow: { status: string; reply: ChatResponse | null; approval: ApprovalRequestView | null; approvals: ApprovalRecordView[] },
): Turn {
  const decision = decisionFrom(workflow.approvals);
  const waiting = workflow.status === "awaiting_approval";
  return {
    ...turn,
    reply: workflow.reply ?? turn.reply,
    answer: workflow.reply?.answer ?? turn.answer,
    request: waiting ? (workflow.approval ?? workflow.reply?.approval ?? null) : turn.request,
    pauseAnswer: waiting ? (workflow.reply?.answer ?? turn.answer) : decision ? turn.pauseAnswer : null,
    decision,
  };
}

/** Whether this role may decide a request that needs at least `required`. */
export function canDecide(role: string, required: string): boolean {
  if (role === "owner") return true;
  if (role === "staff") return required === "staff";
  return false;
}
