import { describe, it, expect } from "vitest";
import { parseRichText, type RichSegment } from "./richText";

/** Reconstruct the source text from segments. Only the backtick delimiters of a
 *  `code` span are consumed by the parse (the renderer re-adds visual styling,
 *  not literal backticks), so we re-add them here; everything else is verbatim.
 *  This proves the parse is a loss-less partition — no prose dropped or dup'd. */
function roundtrips(text: string) {
  return parseRichText(text)
    .map((s) => (s.type === "code" ? `\`${s.value}\`` : s.value))
    .join("");
}

const types = (segs: RichSegment[]) => segs.map((s) => s.type);

describe("parseRichText — rich agent output (#31)", () => {
  it("leaves plain prose as a single text segment", () => {
    const segs = parseRichText("just some normal words here");
    expect(types(segs)).toEqual(["text"]);
  });

  it("extracts inline `code` as a code segment", () => {
    const segs = parseRichText("call `doThing()` to start");
    expect(types(segs)).toEqual(["text", "code", "text"]);
    expect(segs[1]).toMatchObject({ type: "code", value: "doThing()" });
  });

  it("turns a bare http(s) URL into a url segment with href", () => {
    const segs = parseRichText("see https://example.com/docs for more");
    const url = segs.find((s) => s.type === "url");
    expect(url).toMatchObject({
      type: "url",
      value: "https://example.com/docs",
      href: "https://example.com/docs",
    });
  });

  it("recognises a source ref like AudioProcessor.kt:25", () => {
    const segs = parseRichText("look at AudioProcessor.kt:25 closely");
    const ref = segs.find((s) => s.type === "ref");
    expect(ref).toMatchObject({
      type: "ref",
      path: "AudioProcessor.kt",
      line: 25,
    });
  });

  it("recognises a pathed ref like src/foo.py:12", () => {
    const ref = parseRichText("edit src/foo.py:12 now").find((s) => s.type === "ref");
    expect(ref).toMatchObject({ type: "ref", path: "src/foo.py", line: 12 });
  });

  it("treats a backticked source ref as a ref, not code", () => {
    const segs = parseRichText("open `AudioProcessor.kt:25`");
    const ref = segs.find((s) => s.type === "ref");
    expect(ref).toMatchObject({ type: "ref", path: "AudioProcessor.kt", line: 25 });
    expect(types(segs)).not.toContain("code");
  });

  it("unwraps a parenthesised ref, keeping the parens as text", () => {
    const segs = parseRichText("the bug (src/a.ts:9) is here");
    const ref = segs.find((s) => s.type === "ref");
    expect(ref).toMatchObject({ path: "src/a.ts", line: 9 });
    // The parens are preserved in surrounding text (loss-less).
    expect(roundtrips("the bug (src/a.ts:9) is here")).toBe("the bug (src/a.ts:9) is here");
  });

  it("parses a ref with no line number", () => {
    const ref = parseRichText("see src/foo.py for details").find((s) => s.type === "ref");
    expect(ref).toMatchObject({ type: "ref", path: "src/foo.py", line: null });
  });

  it("does NOT mistake English colons for a ref", () => {
    const segs = parseRichText("Step 2:25 was confusing");
    expect(types(segs)).toEqual(["text"]);
  });

  it("is loss-less: concatenated segments equal the input", () => {
    const input = "fix `f()` at src/a.ts:3 and read https://x.io/y (also web.py:1)";
    expect(roundtrips(input)).toBe(input);
  });

  it("handles an unmatched backtick as plain text", () => {
    const segs = parseRichText("a lone ` backtick");
    expect(roundtrips("a lone ` backtick")).toBe("a lone ` backtick");
    expect(types(segs)).not.toContain("code");
  });
});
