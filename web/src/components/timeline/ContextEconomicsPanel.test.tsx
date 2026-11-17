import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { SessionContextEconomics } from "../../domain/models";
import { ContextEconomicsPanel } from "./ContextEconomicsPanel";

const base: SessionContextEconomics = {
  recommendation: "unavailable", threshold: .8, coldWriteUsd: null, warmReadUsd: null,
  compactions: [], recoveries: [], proposals: [],
};

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

it("preserves unknown recovery buckets and provider-specific input semantics", () => {
  const html = renderToStaticMarkup(<ContextEconomicsPanel economics={{ ...base,
    recoveries: [{ taskId: "partial", status: "completed", observedUsd: .47,
      inputTokens: 100, outputTokens: null, cacheReadTokens: null, cacheCreationTokens: null }],
  }} />);
  expect(html).toContain("Input 100");
  expect(html).toContain("cache read -, cache write -, output -");
  expect(html).not.toContain("Uncached input");
});
