import { useState } from "react";
import type { ReactNode } from "react";
import { Badge, Spinner } from "@/components/ui";
import { cn } from "@/lib/cn";
import { toolLabel } from "./model";
import type { ToolTurn } from "./model";

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const yuan = (v: unknown) => (typeof v === "number" ? `¥${v.toFixed(2)}` : "—");
const str = (v: unknown) => (v == null || v === "" ? "—" : String(v));

const ORDER_STATUS: Record<string, { label: string; tone: "brand" | "warn" | "neutral" | "danger" }> = {
  pending: { label: "待发货", tone: "warn" },
  shipped: { label: "已发货", tone: "brand" },
  delivered: { label: "已签收", tone: "neutral" },
  cancelled: { label: "已取消", tone: "danger" },
  refunding: { label: "退款中", tone: "warn" },
  in_transit: { label: "运输中", tone: "brand" },
};

function StatusBadge({ value }: { value: unknown }) {
  const s = ORDER_STATUS[String(value)];
  return <Badge tone={s?.tone ?? "neutral"}>{s?.label ?? str(value)}</Badge>;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 truncate text-sm">{children}</dd>
    </div>
  );
}

function OrderBody({ order }: { order: Obj }) {
  const items = Array.isArray(order.items) ? (order.items as Obj[]) : [];
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-sm">{str(order.order_id)}</span>
        <StatusBadge value={order.status} />
      </div>
      <ul className="space-y-1 text-sm">
        {items.map((item, i) => (
          <li key={i} className="flex justify-between gap-3">
            <span className="truncate">
              {str(item.name)} <span className="text-muted">×{str(item.quantity)}</span>
            </span>
            <span className="shrink-0 tabular-nums">{yuan(item.price)}</span>
          </li>
        ))}
      </ul>
      <dl className="grid grid-cols-2 gap-3 border-t border-line pt-3 sm:grid-cols-4">
        <Field label="实付">{yuan(order.total)}</Field>
        <Field label="下单时间">{str(order.created_at)}</Field>
        <Field label="承运商">{str(order.carrier)}</Field>
        <Field label="预计送达">{str(order.estimated_delivery)}</Field>
      </dl>
    </div>
  );
}

function LogisticsBody({ data }: { data: Obj }) {
  const events = Array.isArray(data.events) ? [...(data.events as Obj[])].reverse() : [];
  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-2 text-sm">
        <span>
          {str(data.carrier)} <span className="font-mono text-muted">{str(data.tracking_number)}</span>
        </span>
        <StatusBadge value={data.status} />
      </div>
      <ol className="relative ml-1.5 space-y-3 border-l border-line pl-4">
        {events.map((e, i) => (
          <li key={i} className="relative">
            <span
              className={cn(
                "absolute -left-[21px] top-1.5 size-2.5 rounded-full border-2 border-surface",
                i === 0 ? "bg-brand" : "bg-line",
              )}
            />
            <p className={cn("text-sm", i === 0 && "font-medium")}>
              {str(e.description)} · {str(e.location)}
            </p>
            <p className="text-xs text-muted">{str(e.time)}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}

function ProductsBody({ products }: { products: Obj[] }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {products.map((p, i) => (
        <div key={i} className="rounded-lg border border-line p-3">
          <div className="flex items-start justify-between gap-2">
            <p className="text-sm font-medium">{str(p.name)}</p>
            <p className="shrink-0 text-sm tabular-nums text-brand">{yuan(p.price)}</p>
          </div>
          <p className="mt-1 line-clamp-2 text-xs text-muted">{str(p.description)}</p>
          <p className="mt-2 text-xs text-muted">库存 {str(p.stock)}</p>
        </div>
      ))}
    </div>
  );
}

function OrderListBody({ orders }: { orders: Obj[] }) {
  return (
    <ul className="divide-y divide-line text-sm">
      {orders.map((o, i) => (
        <li key={i} className="flex items-center justify-between gap-3 py-2">
          <div className="min-w-0">
            <p className="truncate">{str(o.items_summary)}</p>
            <p className="font-mono text-xs text-muted">{str(o.order_id)}</p>
          </div>
          <div className="shrink-0 text-right">
            <p className="tabular-nums">{yuan(o.total)}</p>
            <p className="text-xs text-muted">{str(o.status)}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function KnowledgeBody({ results }: { results: Obj[] }) {
  return (
    <ul className="space-y-2">
      {results.slice(0, 3).map((r, i) => (
        <li key={i} className="text-sm">
          <div className="flex items-center gap-2">
            <span className="font-medium">
              {str(r.doc)}
              {r.section ? ` · ${str(r.section)}` : ""}
            </span>
            {typeof r.score === "number" && <span className="text-xs tabular-nums text-muted">{r.score.toFixed(2)}</span>}
          </div>
          <p className="mt-0.5 line-clamp-2 text-xs text-muted">{str(r.text)}</p>
        </li>
      ))}
    </ul>
  );
}

function RefundBody({ data }: { data: Obj }) {
  return (
    <div className="text-sm">
      <p>{str(data.message ?? data.error)}</p>
      {data.order_id != null && <p className="mt-1 font-mono text-xs text-muted">{str(data.order_id)}</p>}
    </div>
  );
}

function renderBody(turn: ToolTurn): ReactNode | null {
  const out = turn.output;
  if (!isObj(out)) return null;
  if (out.success === false) return <p className="text-sm text-danger">{str(out.error)}</p>;
  switch (turn.name) {
    case "query_order":
      return isObj(out.order) ? <OrderBody order={out.order} /> : null;
    case "query_logistics":
      return isObj(out.logistics) ? <LogisticsBody data={out.logistics} /> : null;
    case "query_product":
      return Array.isArray(out.products) && out.products.length ? <ProductsBody products={out.products as Obj[]} /> : null;
    case "list_user_orders":
      return Array.isArray(out.orders) ? <OrderListBody orders={out.orders as Obj[]} /> : null;
    case "search_knowledge":
      return Array.isArray(out.results) && out.results.length ? <KnowledgeBody results={out.results as Obj[]} /> : null;
    case "apply_refund":
      return <RefundBody data={out} />;
    default:
      return null;
  }
}

export function ToolCard({ turn }: { turn: ToolTurn }) {
  const body = renderBody(turn);
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const hasRich = body !== null;
  const expanded = hasRich ? !open : open; // rich cards start open, generic ones collapsed

  return (
    <div className="rounded-xl border border-line bg-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-muted"
        aria-expanded={expanded}
      >
        {turn.status === "running" ? (
          <Spinner className="text-brand" />
        ) : (
          <span className={cn("size-1.5 rounded-full", turn.status === "failed" ? "bg-danger" : "bg-brand")} />
        )}
        <span className="shrink-0 font-medium whitespace-nowrap text-ink">{toolLabel(turn.name)}</span>
        <span className="truncate font-mono">{Object.values(turn.args).filter((v) => typeof v === "string").join(" · ")}</span>
        <span className="ml-auto shrink-0 whitespace-nowrap">{expanded ? "收起" : "展开"}</span>
      </button>
      {expanded && (
        <div className="border-t border-line px-3 py-3">
          {body}
          {(!hasRich || raw) && (
            <pre className="max-h-64 overflow-auto rounded-lg bg-sunken p-2 font-mono text-xs whitespace-pre-wrap">
              {JSON.stringify(turn.output ?? turn.args, null, 2)}
            </pre>
          )}
          {hasRich && (
            <button type="button" className="mt-2 text-xs text-muted hover:text-ink" onClick={() => setRaw((v) => !v)}>
              {raw ? "隐藏原始数据" : "查看原始数据"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
