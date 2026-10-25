import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { CoverageChip } from "./CoverageChip";

it("never calls rounded coverage fully priced while unpriced tokens exist", () => {
  for (const coveragePct of [99.9, 100]) {
    const markup = renderToStaticMarkup(<CoverageChip coveragePct={coveragePct} unpricedTokens={1} />);
    expect(markup).not.toContain("fully priced");
    expect(markup).not.toContain("all tokens priced");
    expect(markup).toContain("1 unpriced tokens");
  }
  expect(renderToStaticMarkup(<CoverageChip coveragePct={100} unpricedTokens={0} />)).toContain("fully priced");
});
