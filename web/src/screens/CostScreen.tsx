/**
 * CostScreen — the A65 Cost tab (operator-requested spend visibility). A
 * read-only window into the cost read-model: a day-range filter (24h/48h/7d/30d,
 * default 7d) + project filter drive every figure on the tab. The honest
 * accounting is front and center: known USD always carries its coverage %, and
 * unattributed/unpriced tokens are stated, never hidden.
 *
 * Sections: headline estimate → spend by project → spend by model → top
 * sessions. A top session that belongs to a case drills into its
 * manager-vs-workers breakdown (the operator's explicit ask).
 */
import { useMemo, useState } from "react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { CostRangePicker } from "../components/cost/CostRangePicker";
import { CostProjectFilter } from "../components/cost/CostProjectFilter";
import { CostTotalCard } from "../components/cost/CostTotalCard";
import { SpendRows } from "../components/cost/SpendRows";
import { TopSpenders } from "../components/cost/TopSpenders";
import { CaseCostSheet } from "../components/cost/CaseCostSheet";
import { useCostExplorer, useCostTop, useCostProjects } from "../hooks/useCost";
import { useSessionAffiliations } from "../hooks/useWork";
import {
  DEFAULT_COST_RANGE,
  rangeToFromTo,
  projectLabel,
  type CostRangeKey,
} from "../lib/costPresentation";
import type { CostBucket } from "../domain/cost";

const EMPTY_BUCKET: CostBucket = {
  tokens: { input: 0, output: 0, cacheRead: 0, cacheCreation: 0, total: 0 },
  usd: { known: 0, unpricedTokens: 0, coveragePct: 100 },
};

function SkeletonBlock() {
  return (
    <div className="card-elev animate-pulse rounded-xl px-4 py-3.5">
      <div className="h-3.5 w-32 rounded bg-surface-2" />
      <div className="mt-2.5 h-6 w-24 rounded bg-surface-2" />
      <div className="mt-2 h-3 w-2/3 rounded bg-surface-2" />
    </div>
  );
}

export function CostScreen() {
  const [range, setRange] = useState<CostRangeKey>(DEFAULT_COST_RANGE);
  const [repoPath, setRepoPath] = useState<string | null>(null);
  const [drillCase, setDrillCase] = useState<string | null>(null);

  const { from, to } = useMemo(() => rangeToFromTo(range), [range]);
  const window = { from, to, repoPath: repoPath ?? undefined };

  const byProject = useCostExplorer({ dimension: "project", granularity: "none", ...window });
  const byModel = useCostExplorer({ dimension: "model", granularity: "none", ...window, limit: 30 });
  const top = useCostTop({ by: "usd", limit: 10, ...window });
  const projects = useCostProjects({ from, to });
  const { index: affiliations } = useSessionAffiliations();

  const loading =
    (byProject.isLoading && !byProject.data) ||
    (top.isLoading && !top.data);
  const error = byProject.error ?? top.error;

  const projectRows = byProject.data?.series ?? [];
  const modelRows = byModel.data?.series ?? [];
  const maxProjectUsd = Math.max(0, ...projectRows.map((r) => r.usd.known));
  const maxModelUsd = Math.max(0, ...modelRows.map((r) => r.usd.known));

  return (
    <div className="pb-8">
      <CompactTopBar
        title="Cost"
        subtitle="Estimated spend · the read-model, not an invoice"
      />

      <div className="space-y-2 px-4 pt-4">
        <CostRangePicker value={range} onChange={setRange} />
        <CostProjectFilter
          projects={projects.data?.projects ?? []}
          value={repoPath}
          onChange={setRepoPath}
        />
      </div>

      <div className="mt-4 space-y-3">
        {loading && (
          <div className="space-y-3 px-4">
            <SkeletonBlock />
            <SkeletonBlock />
            <SkeletonBlock />
          </div>
        )}

        {error != null && !byProject.data && (
          <p className="px-4 py-10 text-center text-sm text-bad">
            Couldn't load cost data.
          </p>
        )}

        {!loading && !error && (
          <>
            <div className="px-4">
              <CostTotalCard
                totals={byProject.data?.totals ?? EMPTY_BUCKET}
                unattributedTokens={byProject.data?.unattributed.tokens.total ?? 0}
              />
            </div>

            <SectionHeader
              label="Spend by project"
              count={projectRows.length}
            />
            <div className="card-elev mx-4 rounded-xl py-2">
              <SpendRows rows={projectRows} maxUsd={maxProjectUsd} label={projectLabel} />
            </div>

            <SectionHeader label="Spend by model" count={modelRows.length} />
            <div className="card-elev mx-4 rounded-xl py-2">
              <SpendRows rows={modelRows} maxUsd={maxModelUsd} label={(dim) => dim} />
            </div>

            <SectionHeader label="Top sessions" count={top.data?.rows.length ?? 0} />
            <TopSpenders
              rows={top.data?.rows ?? []}
              affiliations={affiliations}
              onPickCase={setDrillCase}
            />
          </>
        )}
      </div>

      {drillCase && <CaseCostSheet flowRunId={drillCase} onClose={() => setDrillCase(null)} />}
    </div>
  );
}
