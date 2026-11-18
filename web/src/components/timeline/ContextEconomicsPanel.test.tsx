import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { SessionContextEconomics } from "../../domain/models";
import { ContextEconomicsPanel } from "./ContextEconomicsPanel";
import { contextTokens } from "./SessionTurns";

const base: SessionContextEconomics = {
  recommendation: "unavailable", threshold: .8, coldWriteUsd: null, warmReadUsd: null,
  compactions: [], recoveries: [], proposals: [],
};

it("does not revive an unknown final context from an earlier aggregate", () => {
  expect(contextTokens({ turn_exit_context_tokens: null, peak_context_tokens: 160000, context_tokens: 325300 })).toBeNull();
  expect(contextTokens({ turn_exit_context_tokens: 0, context_tokens: 325300 })).toBe(0);
});

it("offers compaction only when the backend recommendation warrants it", () => {
  expect(renderToStaticMarkup(<ContextEconomicsPanel economics={base} />)).not.toContain("Consider /compact");
  expect(renderToStaticMarkup(<ContextEconomicsPanel economics={{ ...base, recommendation: "compact" }} />))
    .toContain("Consider /compact: context is at or above 80%");
});

it("keeps unknown benefit and measured zero cost distinct", () => {
  const html = renderToStaticMarkup(<ContextEconomicsPanel economics={{ ...base,
    compactions: [{ taskId: "c1", status: "completed", reclaimedTokens: null, reclaimedRatio: null,
      avoidedReadUsdPerRequest: null, observedCompactionUsd: 0, breakEvenWarmRequests: null }],
  }} />);
  expect(html).toContain("Benefit awaiting context evidence");
  expect(html).toContain("$0.0000");
  expect(html).not.toContain("break-even:");
});

it("keeps an undefined reclaimed fraction distinct from zero percent", () => {
  const html = renderToStaticMarkup(<ContextEconomicsPanel economics={{ ...base,
    compactions: [{ taskId: "empty", status: "completed", reclaimedTokens: 0, reclaimedRatio: null,
      avoidedReadUsdPerRequest: 0, observedCompactionUsd: 0, breakEvenWarmRequests: null }],
  }} />);
  expect(html).toContain("percentage unknown");
});

it("shows the linked recovery, measured cost and the cold alternative", () => {
  const html = renderToStaticMarkup(<ContextEconomicsPanel economics={{ ...base,
    recoveries: [{ taskId: "r1", status: "completed", priorSessionId: "old", sessionId: "new",
      observedUsd: .47, coldResumeUsd: 1, inputTokens: 100, outputTokens: 1000,
      cacheReadTokens: 0, cacheCreationTokens: 10000, contextUsedRatio: .1 }],
  }} />);
  expect(html).toContain("$0.4700");
  expect(html).toContain("old → new");
  expect(html).toContain("$1.0000");
  expect(html).toContain("Context 10.0%");
});


it("shows a failed recovery read rather than presenting an empty history", () => {
  expect(renderToStaticMarkup(<ContextEconomicsPanel economics={{ ...base, recoveryCoverage: "unavailable" }} />))
    .toContain("Recovery evidence unavailable");
});

it("shows unavailable compaction evidence and labels estimated recovery cost", () => {
  const html = renderToStaticMarkup(<ContextEconomicsPanel economics={{ ...base,
    compactionCoverage: "unavailable", recoveries: [{ taskId: "r1", status: "completed",
      observedUsd: .47, costBasis: "recorded_tokens_api_equivalent_5m_write" }],
  }} />);
  expect(html).toContain("Compaction evidence unavailable");
  expect(html).toContain("estimated usage cost (5-minute cache write)");
  expect(html).not.toContain("provider-reported cost");
});

it("preserves unknown recovery buckets and provider-specific input semantics", () => {
  const html = renderToStaticMarkup(<ContextEconomicsPanel economics={{ ...base,
    recoveries: [{ taskId: "partial", status: "completed", observedUsd: .47,
      inputTokens: 100, outputTokens: null, cacheReadTokens: null, cacheCreationTokens: null }],
  }} />);
  expect(html).toContain("Input 100");
  expect(html).toContain("cache read -, cache write -, output -");
  expect(html).not.toContain("Uncached input");
});
