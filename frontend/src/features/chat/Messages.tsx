import { memo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link } from "react-router-dom";
import type { AgentResponse, ApprovalAction } from "@/api/types";
import { Badge, Button, Spinner } from "@/components/ui";
import { cn } from "@/lib/cn";
import { ToolCard } from "./ToolCard";
import { AGENT_LABELS, INTENT_LABELS, toolLabel } from "./model";
import type { LiveRun, Turn } from "./model";

const Reply = memo(function Reply({ text, streaming }: { text: string; streaming?: boolean }) {
  return (
    <div className={cn("prose-reply text-[15px] leading-relaxed", streaming && "caret")}>
      <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
    </div>
  );
});

function Meta({ response }: { response: AgentResponse }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <Badge tone="brand">{INTENT_LABELS[response.intent] ?? response.intent}</Badge>
      <Badge>置信度 {Math.round(response.confidence * 100)}%</Badge>
      {response.requires_human && <Badge tone="warn">已转人工</Badge>}
    </div>
  );
}

function Avatar() {
  return (
    <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-full bg-brand text-sm text-brand-ink">夕</span>
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-brand px-4 py-2.5 text-[15px] whitespace-pre-wrap text-brand-ink">
        {text}
      </div>
    </div>
  );
}

export function TurnView({ turn }: { turn: Turn }) {
  if (turn.kind === "user") return <UserBubble text={turn.text} />;
  if (turn.kind === "tool")
    return (
      <div className="pl-11">
        <ToolCard turn={turn} />
      </div>
    );
  return (
    <div className="flex gap-3">
      <Avatar />
      <div className="min-w-0 flex-1">
        <Reply text={turn.text} />
        {turn.response && <Meta response={turn.response} />}
        {turn.response?.follow_up_question && (
          <p className="mt-2 text-sm text-muted">{turn.response.follow_up_question}</p>
        )}
      </div>
    </div>
  );
}

export function ApprovalBanner({
  sessionId,
  actions,
  canApprove,
  onWithdraw,
}: {
  sessionId: string;
  actions: ApprovalAction[];
  canApprove: boolean;
  onWithdraw?: () => void;
}) {
  if (!actions.length) return null;
  return (
    <div className="rounded-xl border border-warn/30 bg-warn-soft p-4 text-sm">
      <p className="font-medium text-warn">退款申请等待人工审核</p>
      <ul className="mt-2 space-y-1">
        {actions.map((a) => (
          <li key={a.call_id} className="flex flex-wrap gap-x-3">
            <span className="font-mono">{a.order_id ?? a.name}</span>
            <span>{a.amount == null ? "金额待核验" : `¥${a.amount.toFixed(2)}`}</span>
            {a.reason && <span className="text-muted">原因：{a.reason}</span>}
          </li>
        ))}
      </ul>
      <div className="mt-3 flex flex-wrap gap-2">
        {canApprove && (
          <Link to={`/approvals/${sessionId}`}>
            <Button>去审批</Button>
          </Link>
        )}
        {onWithdraw && (
          <Button variant="ghost" onClick={onWithdraw}>
            撤回申请
          </Button>
        )}
      </div>
      {!canApprove && <p className="mt-2 text-xs text-muted">审核员处理后，结果会自动出现在这里。</p>}
    </div>
  );
}

export function LiveRunView({ run, onDismiss }: { run: LiveRun; onDismiss: () => void }) {
  const runningTool = [...run.tools].reverse().find((t) => t.status === "running");
  return (
    <div className="space-y-4">
      {run.userText && <UserBubble text={run.userText} />}
      {run.tools.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pl-11">
          {run.tools.map((t, i) => (
            <Badge key={i} tone={t.status === "failed" ? "danger" : t.status === "running" ? "brand" : "neutral"}>
              {t.status === "running" && <Spinner className="size-3" />}
              {toolLabel(t.name)}
              {t.attempt > 1 && ` · 重试 ${t.attempt}`}
            </Badge>
          ))}
        </div>
      )}
      <div className="flex gap-3">
        <Avatar />
        <div className="min-w-0 flex-1">
          {run.agent && <p className="mb-1 text-xs text-muted">{AGENT_LABELS[run.agent] ?? run.agent} 正在处理</p>}
          {run.text ? (
            <Reply text={run.text} streaming={run.status === "streaming"} />
          ) : run.status === "failed" ? null : run.status === "awaiting_approval" ? (
            <p className="text-sm text-muted">已提交审核，等待审核员处理…</p>
          ) : (
            <p className="flex items-center gap-2 text-sm text-muted">
              <Spinner />
              {runningTool ? `正在${toolLabel(runningTool.name)}…` : "正在思考…"}
            </p>
          )}
          {run.handoff && <Badge tone="warn" className="mt-2">已转人工</Badge>}
          {run.status === "failed" && (
            <div className="mt-2 rounded-lg bg-danger-soft px-3 py-2 text-sm text-danger">
              {run.error ?? "Agent 执行失败"}
              <button className="ml-3 underline" onClick={onDismiss}>
                知道了
              </button>
            </div>
          )}
          {run.status === "cancelled" && <p className="mt-2 text-sm text-muted">已停止生成</p>}
        </div>
      </div>
    </div>
  );
}
