import type { Metadata } from "next";

import { ApprovalsInbox } from "@/components/ApprovalsInbox";

export const metadata: Metadata = { title: "Approvals" };

export default function ApprovalsPage() {
  return <ApprovalsInbox />;
}
