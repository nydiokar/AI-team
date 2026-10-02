import { describe, expect, it } from "vitest";
import { NEW_SESSION_BACKENDS, retiredBackendReason } from "./backends";

describe("retiredBackendReason", () => {
  it("names the OpenCode CLI backend as retired and steers to opencode-server", () => {
    expect(retiredBackendReason("opencode")).toMatch(/OpenCode Server/);
    expect(retiredBackendReason(" OpenCode ")).not.toBeNull();
  });

  it("leaves live backends alone", () => {
    for (const backend of ["claude", "codex", "opencode-server", "", null, undefined]) {
      expect(retiredBackendReason(backend)).toBeNull();
    }
  });
});

describe("NEW_SESSION_BACKENDS", () => {
  it("never offers a retired backend for a new session", () => {
    const ids = NEW_SESSION_BACKENDS.map((b) => b.id);
    expect(ids).toContain("opencode-server");
    for (const id of ids) expect(retiredBackendReason(id)).toBeNull();
    expect(ids).not.toContain("opencode");
  });
});
