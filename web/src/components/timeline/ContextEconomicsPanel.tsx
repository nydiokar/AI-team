import type { SessionContextEconomics } from "../../domain/models";
import { compactTokens } from "./SessionTurns";

const dollars = (value: number | null) => value == null ? "unknown" : `$${value.toFixed(4)}`;

export function ContextEconomicsPanel({ economics: e }: { economics: SessionContextEconomics }) {
  return <div className="px-4 py-2 text-xs text-ink-muted space-y-2">
    {e.recommendation === "compact" && <p role="status" className="text-amber-300">
      Consider /compact: context is at or above {(e.threshold * 100).toFixed(0)}%.
      Compaction can discard detail; its cost and benefit need observation.
    </p>}
    {e.coldWriteUsd != null && <p>Current context: cold write {dollars(e.coldWriteUsd)};
      warm read {dollars(e.warmReadUsd)}. API equivalents, excluding new input and output.</p>}
    {e.recoveryCoverage === "unavailable" && <p role="status">Recovery evidence unavailable — refresh to retry.</p>}
    {e.compactionCoverage === "unavailable" && <p role="status">Compaction evidence unavailable — refresh to retry.</p>}
    {(e.compactions.length > 0 || e.recoveries.length > 0 || e.proposals.length > 0) && <details>
      <summary>Context and recovery evidence (latest 500 turns/tasks)</summary>
      {e.compactions.map(row => <p key={row.taskId}>
        Compaction {row.taskId}: {row.status}. {row.reclaimedTokens == null
          ? "Benefit awaiting context evidence."
          : `${compactTokens(row.reclaimedTokens)} tokens reclaimed (${row.reclaimedRatio == null ? "percentage unknown" : `${(row.reclaimedRatio * 100).toFixed(1)}%`}); estimated avoided warm read ${dollars(row.avoidedReadUsdPerRequest)}/request.`}
        {" "}Recorded compaction cost: {dollars(row.observedCompactionUsd)}.
        {row.breakEvenWarmRequests != null && ` Estimated warm-read break-even, including retained rewrite: ${row.breakEvenWarmRequests} requests.`}
      </p>)}
      {e.recoveries.map(row => <p key={row.taskId}>Recovery {row.taskId}: {row.status};
        {row.costBasis === "provider_reported" ? "provider-reported cost" : "estimated usage cost (5-minute cache write)"} {dollars(row.observedUsd)}.
        {row.priorSessionId && <> Session {row.priorSessionId} → {row.sessionId}.</>}
        {row.coldResumeUsd != null && <> Old-context cold alternative: {dollars(row.coldResumeUsd)}.</>}
        {[row.inputTokens, row.outputTokens, row.cacheReadTokens, row.cacheCreationTokens].some(value => value != null) && <> Input {compactTokens(row.inputTokens)},
          cache read {compactTokens(row.cacheReadTokens)}, cache write {compactTokens(row.cacheCreationTokens)},
          output {compactTokens(row.outputTokens)}.</>}
        {row.contextUsedRatio != null && <> Context {(row.contextUsedRatio * 100).toFixed(1)}%.</>}
      </p>)}
      {e.proposals.map(row => <p key={row.approvalId}>Proposal {row.approvalId}: {row.status};
        old-context cold resume estimate {dollars(row.coldResumeUsd)}.
        {row.contextTokens != null && <> Context {compactTokens(row.contextTokens)} tokens.</>}
        {row.contextUsedRatio != null && <> {(row.contextUsedRatio * 100).toFixed(1)}% of window.</>}
        {" "}Fresh boot cost unknown until recorded.</p>)}
      <p>Costs are API equivalents. Avoided cost is a scenario, not realized savings.</p>
    </details>}
  </div>;
}
