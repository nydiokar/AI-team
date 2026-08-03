/**
 * CostAlertBanner — the P3 budget/burn-rate alert surface on the Cost tab.
 * Honest three-state: budgets unarmed (knob not set), within budget, or fired.
 * Alerts are billable-USD only; a fired card shows value vs budget + scope.
 * Enforcement is OFF by default and only surfaces the existing governor lever.
 */
import { TriangleAlert, ShieldCheck } from "lucide-react";
import { formatUsd } from "../../lib/costPresentation";
import type { CostAlertRule, CostAlerts } from "../../domain/cost";

const RULE_LABEL: Record<CostAlertRule, string> = {
  daily_budget: "Daily budget",
  session_burn: "Session burn",
  case_total: "Case total",
};

export function CostAlertBanner({ alerts }: { alerts: CostAlerts | undefined }) {
  if (!alerts) return null;

  if (!alerts.enabled) {
    return (
      <div className="rounded-xl bg-surface-2/60 px-4 py-2.5 text-[11px] text-ink-muted">
        Budgets not armed — set a COST_ALERT_* env knob to enable USD burn alerts.
      </div>
    );
  }

  if (alerts.alerts.length === 0) {
    return (
      <div className="rounded-xl bg-ok/10 px-4 py-2.5 text-[11px] text-ok">
        Within configured budgets.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {alerts.alerts.map((a) => (
        <div
          key={a.rule}
          className="rounded-xl bg-warm-dim/70 px-4 py-2.5 ring-1 ring-warn/30"
        >
          <div className="flex items-center gap-2 text-[12px] font-medium text-warn">
            <TriangleAlert className="size-3.5 shrink-0" />
            <span className="truncate">{RULE_LABEL[a.rule]}</span>
            <span className="ml-auto shrink-0 tabular-nums">{a.pct}% of budget</span>
          </div>
          <div className="mt-0.5 truncate text-[11px] tabular-nums text-ink-muted">
            {formatUsd(a.valueUsd)} spent · {formatUsd(a.budgetUsd)} budget · {a.scope}
          </div>
        </div>
      ))}
      {alerts.enforcement.enabled && (
        <div className="flex items-center gap-1.5 px-1 text-[11px] text-ink-muted">
          <ShieldCheck className="size-3" />
          Enforcement surfaces the SDK governor ceiling
          {alerts.enforcement.governorSdkMaxBudgetUsd != null
            ? ` (${formatUsd(alerts.enforcement.governorSdkMaxBudgetUsd)})`
            : " (none configured)"}
        </div>
      )}
    </div>
  );
}
