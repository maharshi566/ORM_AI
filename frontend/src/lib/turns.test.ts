// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";

import type { ChatResponse, SessionResponse } from "../types/api.ts";
import {
  applyStageEvent,
  canDecide,
  decisionFrom,
  emptyRail,
  pauseRail,
  railFromReply,
  stopRail,
  turnsFromSession,
  withWorkflow,
} from "./turns.ts";

test("live events light up a stage, and two parallel agents finish it together", () => {
  let rail = emptyRail();
  rail = applyStageEvent(rail, { stage: "retrieval", agent: "data_retrieval", state: "started" });
  rail = applyStageEvent(rail, { stage: "retrieval", agent: "knowledge", state: "started" });
  rail = applyStageEvent(rail, {
    stage: "retrieval",
    agent: "data_retrieval",
    state: "finished",
    status: "success",
    latency_ms: 40,
  });

  assert.equal(rail.stages.retrieval.state, "active");

  rail = applyStageEvent(rail, {
    stage: "retrieval",
    agent: "knowledge",
    state: "finished",
    status: "success",
    summary: "2 searches",
    latency_ms: 60,
  });

  assert.equal(rail.stages.retrieval.state, "done");
  assert.equal(rail.stages.retrieval.latencyMs, 100);
  assert.equal(rail.stages.retrieval.summary, "2 searches");
  assert.equal(rail.stages.triage.state, "waiting");
});

test("a failed agent marks its stage, and the supervisor's note is kept", () => {
  let rail = emptyRail();
  rail = applyStageEvent(rail, { stage: "routing", agent: "supervisor", state: "finished", summary: "next: knowledge" });
  rail = applyStageEvent(rail, { stage: "investigation", agent: "investigation", state: "started" });
  rail = applyStageEvent(rail, { stage: "investigation", agent: "investigation", state: "finished", status: "error" });

  assert.equal(rail.supervisor, "next: knowledge");
  assert.equal(rail.stages.investigation.state, "failed");
  assert.equal(pauseRail(rail).stages.approval.state, "paused");
});

function step(agent: string, status = "success") {
  return { agent, status, model: null, latency_ms: 10, input_tokens: 0, output_tokens: 0, summary: `${agent} ran`, error: null };
}

test("a paused reply waits at the approval stage; the stages after it are still to come", () => {
  const rail = railFromReply({
    status: "awaiting_approval",
    agents: [step("triage"), step("supervisor"), step("data_retrieval"), step("investigation")],
  });

  assert.equal(rail.stages.triage.state, "done");
  assert.equal(rail.stages.approval.state, "paused");
  assert.equal(rail.stages.action.state, "waiting");
  assert.equal(rail.supervisor, "supervisor ran");
});

test("a finished reply skips the stages it never needed", () => {
  const rail = railFromReply({
    status: "completed",
    agents: [step("triage"), step("knowledge"), step("respond"), step("validate"), step("finalize")],
  });

  assert.equal(rail.stages.investigation.state, "skipped");
  assert.equal(rail.stages.response.state, "done");
  assert.equal(rail.stages.response.latencyMs, 20);
});

const SESSION: SessionResponse = {
  session_id: "s1",
  shop_id: "SHOP-001",
  user_id: "USR-002",
  title: null,
  created_at: "2026-10-10T10:00:00Z",
  last_active_at: "2026-10-10T10:05:00Z",
  workflows: [],
  messages: [
    { role: "user", content: "How much does CUST-0001 owe?", created_at: "t1", workflow_id: "w1" },
    { role: "assistant", content: "Rs 1,200.", created_at: "t2", workflow_id: "w1" },
    { role: "user", content: "Please process the return of SALE-005598.", created_at: "t3", workflow_id: "w2" },
    { role: "assistant", content: "I need a decision before I do anything.", created_at: "t4", workflow_id: "w2" },
    { role: "user", content: "[decision by USR-002] approve process_return", created_at: "t5", workflow_id: "w2" },
    { role: "assistant", content: "Done: Rs 190 refunded.", created_at: "t6", workflow_id: "w2" },
  ],
};

test("a stored conversation becomes one entry per request, decisions folded in", () => {
  const turns = turnsFromSession(SESSION);

  assert.equal(turns.length, 2);
  assert.equal(turns[0].answer, "Rs 1,200.");
  assert.equal(turns[1].question, "Please process the return of SALE-005598.");
  assert.equal(turns[1].pauseAnswer, "I need a decision before I do anything.");
  assert.equal(turns[1].answer, "Done: Rs 190 refunded.");
  assert.equal(turns[1].live, null);
});

test("the workflow record adds the decision and the full reply", () => {
  const [, turn] = turnsFromSession(SESSION);
  const reply = { answer: "Done: Rs 190 refunded.", status: "completed", approval: null } as unknown as ChatResponse;

  const filled = withWorkflow(turn, {
    status: "completed",
    reply,
    approval: null,
    approvals: [
      {
        approval_id: "a",
        tool: "process_return",
        status: "approved",
        required_role: "staff",
        decided_by: "USR-002",
        decided_at: "2026-10-10T10:04:00Z",
        decision_note: "Has the bill",
      },
    ],
  });

  assert.equal(filled.decision?.status, "approved");
  assert.equal(filled.decision?.by, "USR-002");
  assert.equal(filled.pauseAnswer, "I need a decision before I do anything.");
  assert.equal(filled.reply, reply);
});

test("mixed decisions and who may decide", () => {
  const decision = decisionFrom([
    { approval_id: "a", tool: "x", status: "approved", required_role: "staff", decided_by: "U", decided_at: null, decision_note: null },
    { approval_id: "b", tool: "y", status: "rejected", required_role: "staff", decided_by: "U", decided_at: null, decision_note: null },
  ]);

  assert.equal(decision?.status, "mixed");
  assert.equal(decisionFrom([]), null);
  assert.equal(canDecide("staff", "staff"), true);
  assert.equal(canDecide("staff", "owner"), false);
  assert.equal(canDecide("owner", "owner"), true);
  assert.equal(canDecide("admin", "staff"), false);
});

test("a run that broke off marks the working stage as failed", () => {
  let rail = applyStageEvent(emptyRail(), { stage: "triage", agent: "triage", state: "started" });
  rail = stopRail(rail);

  assert.equal(rail.stages.triage.state, "failed");
  assert.equal(rail.stages.retrieval.state, "waiting");
});
