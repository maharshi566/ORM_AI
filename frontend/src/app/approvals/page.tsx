import type { Metadata } from "next";

import { ComingSoon } from "@/components/ComingSoon";

export const metadata: Metadata = { title: "Approvals" };

export default function ApprovalsPage() {
  return (
    <ComingSoon
      title="Approvals"
      phase="Phase 7 · backend in Phase 5"
      description="Anything that moves money or stock waits here for the shopkeeper. Nothing runs until a person approves it."
      features={[
        "Proposed action and the reason for it",
        "Evidence and the shop policy it relies on",
        "Confidence score",
        "Approve, reject or modify",
      ]}
    />
  );
}
