/**
 * Cost analytics over the existing bounded, read-only cost API. Timeline,
 * summary and model mix share one daily dataset; independent panels fail locally.
 */
import { useMemo, useState } from "react";
import { Activity, RefreshCw, FolderOpen, Info } from "lucide-react";
import { CostRangePicker } from "../components/cost/CostRangePicker";
import { CostProjectFilter } from "../components/cost/CostProjectFilter";
import { CostTotalCard } from "../components/cost/CostTotalCard";
import { CostAlertBanner } from "../components/cost/CostAlertBanner";
import { SpendRows } from "../components/cost/SpendRows";
import { TopSpenders } from "../components/cost/TopSpenders";
import { CaseCostSheet } from "../components/cost/CaseCostSheet";
import { CostPanel, CostQueryNotice } from "../components/cost/CostPanel";
import { CostTimeline } from "../components/cost/CostTimeline";
import { CostModelMix } from "../components/cost/CostModelMix";
import { CostTokenMix } from "../components/cost/CostTokenMix";
import { useCostExplorer, useCostTop, useCostProjects, useCostAlerts } from "../hooks/useCost";
import { useSessionAffiliations } from "../hooks/useWork";
import { DEFAULT_COST_RANGE, projectLabel, formatTokens, type CostRangeKey } from "../lib/costPresentation";
import { aggregateModels, utcWindowLabel, formatCostUsd as formatUsd } from "../lib/costAnalytics";
import "../components/cost/cost.css";

export function CostScreen() {
  const [range, setRange] = useState<CostRangeKey>(DEFAULT_COST_RANGE);
  const [repoPath, setRepoPath] = useState<string | null>(null);
  const [sort, setSort] = useState<"usd" | "tokens">("usd");
  const [drillCase, setDrillCase] = useState<string | null>(null);
  const scope = { range, repoPath: repoPath ?? undefined };

  const daily = useCostExplorer({ dimension: "model", granularity: "day", ...scope });
  const byProject = useCostExplorer({ dimension: "project", granularity: "none", ...scope });
  const top = useCostTop({ by: sort, limit: 10, ...scope });
  const projects = useCostProjects({ range });
  const alerts = useCostAlerts();
  const affiliations = useSessionAffiliations();

  const models = useMemo(() => aggregateModels(daily.data?.series ?? []), [daily.data]);
  const projectRows = useMemo(() => [...(byProject.data?.series ?? [])].sort((a, b) => b.usd.known - a.usd.known || b.tokens.total - a.tokens.total), [byProject.data]);
  const maxProjectUsd = Math.max(0, ...projectRows.map((row) => row.usd.known));
  const fetching = [daily, byProject, top, projects, alerts].some((query) => query.isFetching);
  const refresh = () => { for (const query of [daily, byProject, top, projects, alerts]) void query.refetch(); };
  const dailyNotice = <CostQueryNotice loading={daily.isLoading} error={daily.error} hasData={Boolean(daily.data)} onRetry={() => void daily.refetch()} />;

  return (
    <div className="cost-dashboard">
      <header className="cost-page-header">
        <div>
          <div className="mb-3 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.2em] text-accent"><Activity className="size-3.5" aria-hidden="true" />Usage intelligence</div>
          <h1 className="text-[28px] font-semibold leading-tight tracking-tight text-ink">Cost overview<span className="text-accent">.</span></h1>
          <p className="mt-2 text-sm text-ink-muted">Understand your spend. See what drives it.</p>
        </div>
        <button type="button" onClick={refresh} disabled={fetching} className="cost-refresh" aria-label="Refresh cost data">
          <RefreshCw className={fetching ? "size-3.5 animate-spin" : "size-3.5"} aria-hidden="true" />
          <span className="cost-refresh-label">{fetching ? "Updating" : "Refresh"}</span>
        </button>
      </header>

      <div className="cost-toolbar">
        <CostRangePicker value={range} onChange={setRange} />
        <div className="flex min-w-0 items-center gap-2">
          <FolderOpen className="size-4 shrink-0 text-ink-muted" aria-hidden="true" />
          <CostProjectFilter projects={projects.data?.projects ?? []} value={repoPath} onChange={setRepoPath} />
        </div>
      </div>
      {projects.error != null && <CostQueryNotice loading={false} error={projects.error} hasData={Boolean(projects.data)} onRetry={() => void projects.refetch()} />}
      <div className="mb-6 mt-3 flex flex-wrap items-center justify-between gap-2 text-[11px] text-ink-muted">
        <p>{daily.data?.from && daily.data.to ? utcWindowLabel(daily.data.from, daily.data.to) : "Loading selected UTC window…"}</p>
        <p>Rolling window · updates every 15s</p>
      </div>

      <section aria-label="Cost summary">
        {dailyNotice}
        {daily.data && <CostTotalCard totals={daily.data.totals} unattributedTokens={daily.data.unattributed.tokens.total} />}
      </section>

      <div className="cost-primary-grid mt-5">
        <CostPanel title="Spend over time" subtitle="Daily captured usage · session-attributed estimates">
          {!daily.data && dailyNotice}
          {daily.data && <CostTimeline data={daily.data} />}
        </CostPanel>
        <CostPanel title="Model mix" subtitle="Share of listed known USD">
          {!daily.data && dailyNotice}
          {daily.data && <CostModelMix rows={models} />}
        </CostPanel>
      </div>

      <div className="cost-secondary-grid mt-5">
        <CostPanel title="Project allocation" subtitle="Ranked by known USD · select a project to focus"
          action={repoPath ? <button type="button" onClick={() => setRepoPath(null)} className="cost-text-button">Clear filter</button> : undefined}>
          <CostQueryNotice loading={byProject.isLoading} error={byProject.error} hasData={Boolean(byProject.data)} onRetry={() => void byProject.refetch()} />
          {byProject.data && <>
            <SpendRows rows={projectRows.slice(0, 5)} maxUsd={maxProjectUsd} label={projectLabel} onPickProject={setRepoPath} />
            {projectRows.length > 5 && <details className="cost-details mt-5"><summary>View all {projectRows.length} projects</summary><SpendRows rows={projectRows.slice(5)} maxUsd={maxProjectUsd} label={projectLabel} onPickProject={setRepoPath} /></details>}
            <p className="mt-5 text-[11px] text-ink-muted">Bars compare listed projects with the largest known spend.</p>
          </>}
        </CostPanel>
        <CostPanel title="Token composition" subtitle="Volume across input, output and cache">
          {!daily.data && dailyNotice}
          {daily.data && <CostTokenMix tokens={daily.data.totals.tokens} />}
        </CostPanel>
      </div>

      <CostPanel className="mt-5" title="Top sessions" subtitle="Up to 10 sessions in this window · open a session or its case"
        action={<div className="cost-segment" role="group" aria-label="Session ranking">
          {(["usd", "tokens"] as const).map((by) => <button key={by} type="button" aria-pressed={sort === by} onClick={() => setSort(by)}>{by === "usd" ? "Spend" : "Tokens"}</button>)}
        </div>}>
        <CostQueryNotice loading={top.isLoading} error={top.error} hasData={Boolean(top.data)} onRetry={() => void top.refetch()} />
        {affiliations.error != null && <p role="status" className="mt-3 text-xs text-warn">Case links are unavailable. Session links still work.</p>}
        {top.data && <TopSpenders rows={top.data.rows} affiliations={affiliations.index} onPickCase={setDrillCase} />}
      </CostPanel>

      <section aria-label="Gateway budget status" className="mt-5">
        <p className="mb-2 text-[11px] text-ink-muted">Gateway-wide budget status · independent of these filters</p>
        <CostQueryNotice loading={alerts.isLoading} error={alerts.error} hasData={Boolean(alerts.data)} onRetry={() => void alerts.refetch()} />
        <CostAlertBanner alerts={alerts.data} />
      </section>

      <footer className="mt-6 flex items-start gap-2 border-t border-hairline/60 pt-5 text-[11px] leading-relaxed text-ink-muted">
        <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        <div className="space-y-2">
          <p>Estimates from captured usage, subject to API row limits. Unpriced tokens are excluded from USD. These figures are not an invoice. Panels refresh independently.</p>
          {daily.data && daily.data.unattributed.tokens.total > 0 && <p><span className="text-ink-soft">Usage without a session:</span> {formatUsd(daily.data.unattributed.usd.known)} known USD · {formatTokens(daily.data.unattributed.tokens.total)} tokens · {formatTokens(daily.data.unattributed.usd.unpricedTokens)} unpriced. Global for this time window, excluded from the totals above{repoPath ? " and not scoped to this project" : ""}.</p>}
        </div>
      </footer>
      {drillCase && <CaseCostSheet flowRunId={drillCase} onClose={() => setDrillCase(null)} />}
    </div>
  );
}
