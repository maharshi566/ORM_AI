import type { Metadata } from "next";

import { WorkflowRecord } from "@/components/WorkflowRecord";

export const metadata: Metadata = { title: "Request record" };

export default async function WorkflowPage({ params }: PageProps<"/workflows/[id]">) {
  const { id } = await params;
  return <WorkflowRecord workflowId={id} />;
}
