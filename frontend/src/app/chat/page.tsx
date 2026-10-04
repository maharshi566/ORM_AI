import type { Metadata } from "next";

import { ComingSoon } from "@/components/ComingSoon";

export const metadata: Metadata = { title: "Chat" };

export default function ChatPage() {
  return (
    <ComingSoon
      title="Chat"
      phase="Phase 7 · backend in Phase 4"
      description="Ask ORM_AI about the shop in plain language. Each answer shows which agents ran, which records and policies it used, and what it did."
      features={[
        "Conversation and input box",
        "Live workflow stages: triage, retrieval, investigation, validation, response",
        "Sources panel with document, section and excerpt",
        "Tool activity summary",
      ]}
    />
  );
}
