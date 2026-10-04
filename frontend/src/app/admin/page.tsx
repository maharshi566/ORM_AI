import type { Metadata } from "next";

import { ComingSoon } from "@/components/ComingSoon";

export const metadata: Metadata = { title: "Admin" };

export default function AdminPage() {
  return (
    <ComingSoon
      title="Admin and evaluation"
      phase="Phase 7 · data from Phase 8"
      description="See how the agents are performing: every workflow run, its errors and latency, and the evaluation scores."
      features={[
        "Agent executions and errors",
        "Latency per agent and per request",
        "Tool calls and retrieval results",
        "Evaluation scores from the 40-case test set",
      ]}
    />
  );
}
