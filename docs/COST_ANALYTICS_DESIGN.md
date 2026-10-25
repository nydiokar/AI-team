# Cost analytics — reviewed concept

Date: 2026-10-26. Scope: Cost tab, its existing read hooks, and a pricing-coverage arithmetic correction.

## Purpose and visual direction

Answer, in order: what did recorded usage cost, when did it happen, what drove it,
and which session deserves a closer look? Keep the established Workshop graphite
surfaces, Geist typography and cyan actions. Give the numbers space, use a small
consistent chart palette, and let strong hierarchy carry the page.

The layout expands within the existing desktop frame and stacks on phones:

1. Cost overview, UTC window, range/project controls and refresh.
2. Estimated known USD, recorded tokens, cache-read token share and pricing coverage.
3. Daily spend/volume bars, exact accessible table and partial boundary-day labels.
4. Ranked project allocation with project filtering; model mix with explicit values.
5. Input/output/cache token composition, described as volume rather than savings.
6. Ranked sessions with USD/tokens sorting, session links and case-lifetime drilldown.

## Architecture and minimal implementation

Reuse the canonical cost types, adapters, TanStack Query hooks and existing explorer,
top, projects, alerts and case-usage endpoints. Replace the model/none query with
model/day; reuse its rows for the timeline, model aggregation and summary. This
adds no polling fan-out. Project allocation remains an independent project/none
read. SVG/CSS charts require no chart dependency. Use installed Radix Dialog for
the case sheet. No migrations, budget writes, pricing changes or worker changes.

Generate rolling range boundaries inside each fetch, retaining stable range/project
query keys. Remove previous-scope placeholder data. Refresh all visible reads on
request, keep cached values visible during same-scope refresh, and show failures
per panel. Scoped requests use cancellation and a finite deadline.

## Adversarial findings and decisions

- **Coverage bug:** `_sum_aggregates` includes `total` while summing token fields,
  doubling its denominator. Correct it to the explicit total; test mixed and fully
  unpriced aggregates before changing the implementation.
- **Attribution:** headline/chart totals are session-attributed. No-session usage
  is returned separately and ignores the project filter. Show it separately as a
  global-window note, including known USD and tokens; never add it to project totals.
- **Scope:** alerts are gateway-wide. Case usage is lifetime, independent of the
  selected range/project. Label both scopes where seen.
- **UTC:** rolling ranges can contain partial first and last UTC dates. Keep daily
  bars discrete, mark partial buckets, state exact boundaries; no invented hourly
  data, smoothing, previous-period trend or forecast.
- **Unknown versus zero:** no captured tokens is empty; captured tokens with no
  known price is unpriced usage. Coverage and unpriced counts remain visible.
- **API bounds:** explorer SQL limits grouped source rows before pricing; there is
  no completeness flag. Model/day minimizes cardinality; retain bounded requests
  and disclose that estimates reflect returned captured usage, subject to API limits.
  Top totals describe returned sessions only and never fuel headline totals.
- **Projects:** `<no repo path>` cannot be filtered by the existing endpoint.
  Keep its allocation visible but informational; omit it from selectable repo options.
- **Composition:** cache-read share is token volume, never a cache-hit rate or a
  claim about money saved. Model percentages use listed known USD only.
- **Accessibility:** exact values work without hover, charts have names and text
  alternatives, controls work by keyboard/touch, focus is visible, and the case
  dialog traps/restores focus and supports Escape.
- **Resilience:** loading, empty, unpriced, initial error and failed-refresh states
  are distinct. A failed panel must not hide independent successful panels.

## Acceptance evidence

Targeted pure-data and screen tests, the existing cost adapter tests, focused
backend cost tests, strict TypeScript and production build. Browser checks cover
320px phone, tablet and desktop layouts; range/project/sort/metric controls;
exact-value table; case dialog keyboard behavior; partial failures; zero and
unpriced usage. An independent agent reviews the final diff adversarially, and
actionable findings are fixed before shipping. Fixture-driven browser evidence
proves presentation/interaction; a read-only live API check proves the wire shape.
