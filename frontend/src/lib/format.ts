// Plain-language labels and number formats used across the pages.

const DATE_TIME = new Intl.DateTimeFormat("en-IN", {
  day: "numeric",
  month: "short",
  hour: "numeric",
  minute: "2-digit",
});
const DATE = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric" });

export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : DATE_TIME.format(date);
}

export function day(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : DATE.format(date);
}

/** "just now", "5 min ago", "3 h ago", or the date. */
export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const minutes = Math.round((now - then) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return day(iso);
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  if (value >= 10000) return `${(value / 1000).toFixed(0)} s`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)} s`;
  return `${Math.round(value)} ms`;
}

export function count(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString("en-IN");
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  return `${Math.round(value * 100)}%`;
}

export { STAGES, stageOf, type StageId } from "@/lib/turns";

const AGENT_LABEL: Record<string, string> = {
  triage: "Triage",
  supervisor: "Supervisor",
  data_retrieval: "Records",
  knowledge: "Knowledge",
  investigation: "Investigation",
  human_review: "Approval",
  action: "Action",
  respond: "Response",
  clarify: "Clarify",
  validate: "Validator",
  finalize: "Finish",
};

export function agentLabel(agent: string | null | undefined): string {
  if (!agent) return "";
  return AGENT_LABEL[agent] ?? agent.replace(/_/g, " ");
}

const TOOL_LABEL: Record<string, string> = {
  check_supplier_price: "Supplier price",
  get_case_history: "Case history",
  get_customer_account: "Customer account",
  get_low_stock_products: "Low-stock list",
  get_product: "Product",
  get_purchase_orders: "Purchase orders",
  get_sale: "Sale",
  get_sales_summary: "Sales summary",
  get_slow_moving_products: "Slow-moving products",
  get_stock_movements: "Stock movements",
  get_supplier: "Supplier",
  search_customers: "Customer search",
  search_knowledge: "Shop rules search",
  search_products: "Product search",
  create_case: "Open a case",
  resolve_case: "Close a case",
  send_payment_reminder: "Send a payment reminder",
  follow_up_supplier: "Message the supplier",
  create_purchase_order: "Place a purchase order",
  record_stock_adjustment: "Adjust stock",
  update_selling_price: "Change a selling price",
  process_return: "Refund a return",
};

/** "Message the supplier" for follow_up_supplier; unknown tools in words. */
export function toolLabel(tool: string): string {
  if (TOOL_LABEL[tool]) return TOOL_LABEL[tool];
  const words = tool.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const ARGUMENT_LABEL: Record<string, string> = {
  sale_id: "Sale",
  customer_id: "Customer",
  product_id: "Product",
  supplier_id: "Supplier",
  purchase_order_id: "Purchase order",
  case_id: "Case",
  new_selling_price: "New selling price (Rs)",
  quantity_change: "Change in quantity",
  movement_type: "Kind of change",
  reason: "Reason",
  issue: "Problem",
  channel: "Send by",
  lines: "Order lines",
  submit_to_supplier: "Send to the supplier now",
  notes: "Notes",
  title: "Title",
  description: "Description",
  category: "Category",
  resolution: "Resolution",
};

export function argumentLabel(name: string): string {
  if (ARGUMENT_LABEL[name]) return ARGUMENT_LABEL[name];
  const words = name.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function showValue(value: unknown): string {
  if (value === null || value === undefined) return "–";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

type Tone = "done" | "waiting" | "problem" | "quiet";

const STATUS: Record<string, { label: string; tone: Tone }> = {
  completed: { label: "Answered", tone: "done" },
  needs_clarification: { label: "Needs more detail", tone: "quiet" },
  awaiting_approval: { label: "Waiting for approval", tone: "waiting" },
  blocked: { label: "Stopped by a safety check", tone: "problem" },
  failed: { label: "Failed", tone: "problem" },
  running: { label: "Running", tone: "quiet" },
  pending: { label: "Waiting", tone: "waiting" },
  approved: { label: "Approved", tone: "done" },
  modified: { label: "Approved with changes", tone: "done" },
  rejected: { label: "Rejected", tone: "problem" },
  done: { label: "Done", tone: "done" },
  success: { label: "OK", tone: "done" },
  error: { label: "Error", tone: "problem" },
  needs_owner: { label: "Needs the owner", tone: "waiting" },
  proposed: { label: "Proposed", tone: "quiet" },
};

export function statusLabel(status: string | null | undefined): { label: string; tone: Tone } {
  if (!status) return { label: "–", tone: "quiet" };
  return STATUS[status] ?? { label: status.replace(/_/g, " "), tone: "quiet" };
}

export const TONE_CLASS: Record<Tone, string> = {
  done: "bg-stamp-soft text-stamp",
  waiting: "bg-khata-soft text-khata",
  problem: "bg-amber-soft text-amber",
  quiet: "bg-sheet-2 text-ink-2",
};

/** Short, readable slip number for an approval ID (a UUID). */
export function slipNumber(approvalId: string): string {
  return approvalId.replace(/-/g, "").slice(0, 6).toUpperCase();
}
