"use client";

import { useEffect, useState } from "react";

import { RecordId } from "@/components/ui";
import { getBriefing } from "@/lib/api";
import type { BriefingView } from "@/types/api";

const rupees = (value: string) =>
  `Rs ${Number(value).toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;

/**
 * Today's morning briefing for the shop, above the chat: what needs attention, each
 * with a question to ask about it. Hidden when the briefing cannot be loaded (the chat
 * works without it).
 */
export function BriefingCard({ shopId, onAsk, disabled }: { shopId: string; onAsk: (text: string) => void; disabled: boolean }) {
  const [briefing, setBriefing] = useState<BriefingView | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getBriefing(shopId, controller.signal).then(setBriefing, () => setBriefing(null));
    return () => controller.abort();
  }, [shopId]);

  if (!briefing || briefing.shop_id !== shopId) return null;
  const c = briefing.content;
  const day = new Date(`${briefing.briefing_date}T00:00:00`).toLocaleDateString("en-IN", {
    weekday: "long",
    day: "numeric",
    month: "short",
  });

  const rows: { label: string; detail: React.ReactNode; ask?: string }[] = [
    {
      label: c.low_stock_count ? `${c.low_stock_count} low on stock` : "Stock is fine",
      detail: c.low_stock_count ? (
        <>
          {c.low_stock_not_ordered} not yet ordered
          {c.low_stock.length ? ": " : ""}
          {c.low_stock.map((p, i) => (
            <span key={p.product_id}>
              {i ? ", " : ""}
              {p.name} ({p.stock_qty} left{p.on_order ? ", on order" : ""})
            </span>
          ))}
        </>
      ) : (
        "Nothing is below its reorder level."
      ),
      ask: c.low_stock_not_ordered ? "Which products are running low, and should I reorder any of them?" : undefined,
    },
    {
      label: c.overdue_count ? `${c.overdue_count} overdue ${c.overdue_count === 1 ? "payment" : "payments"}` : "No overdue payments",
      detail: c.overdue_count ? (
        <>
          {rupees(c.overdue_total)} in all
          {c.overdue.length ? ": " : ""}
          {c.overdue.map((o, i) => (
            <span key={o.customer_id}>
              {i ? ", " : ""}
              {o.name} {rupees(o.balance)} ({o.days_past_due} days)
            </span>
          ))}
        </>
      ) : (
        "Every credit bill is within its due date."
      ),
      ask: c.overdue[0] ? `How much does ${c.overdue[0].customer_id} owe, and should I send a payment reminder?` : undefined,
    },
  ];
  if (c.late_orders.length) {
    const first = c.late_orders[0];
    rows.push({
      label: `${c.late_orders.length} late ${c.late_orders.length === 1 ? "delivery" : "deliveries"}`,
      detail: c.late_orders.map((o, i) => (
        <span key={o.purchase_order_id}>
          {i ? ", " : ""}
          <RecordId>{o.purchase_order_id}</RecordId> from {o.supplier_name}, {o.days_late} days late
        </span>
      )),
      ask: `Purchase order ${first.purchase_order_id} still has not arrived. What should I do?`,
    });
  }

  return (
    <section aria-labelledby="briefing" className="mb-6 max-w-[60ch] rounded-lg border border-rule bg-sheet-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-rule px-4 py-2.5">
        <h2 id="briefing" className="font-serif font-bold">
          Today&apos;s briefing
        </h2>
        <span className="text-xs text-ink-3">
          {day}
          {briefing.sent_at ? ` · sent on WhatsApp to ${briefing.sent_to}` : ""}
        </span>
      </div>
      <ul className="divide-y divide-rule text-sm">
        {rows.map((row) => (
          <li key={row.label} className="px-4 py-2.5">
            <p className="font-semibold">{row.label}</p>
            <p className="mt-0.5 text-ink-2">{row.detail}</p>
            {row.ask ? (
              <button
                type="button"
                disabled={disabled}
                onClick={() => onAsk(row.ask!)}
                className="mt-1 text-sm font-medium text-khata hover:underline disabled:opacity-60"
              >
                Ask about this →
              </button>
            ) : null}
          </li>
        ))}
        <li className="px-4 py-2.5 text-ink-2">
          {c.yesterday_bills ? `Yesterday: ${c.yesterday_bills} bills, ${rupees(c.yesterday_sales)}` : "Yesterday: no sales recorded"}
          {c.approvals_waiting ? ` · ${c.approvals_waiting} waiting for approval` : ""}
        </li>
      </ul>
    </section>
  );
}
