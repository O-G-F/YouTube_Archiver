import { describe, expect, it } from "vitest";
import { fmtBytes, fmtDuration, fmtUploadDate, statusKind, stateKind } from "../lib/format";

describe("format helpers", () => {
  it("formats bytes", () => {
    expect(fmtBytes(null)).toBe("—");
    expect(fmtBytes(512)).toBe("512 B");
    expect(fmtBytes(2048)).toBe("2.0 KB");
    expect(fmtBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  it("formats duration", () => {
    expect(fmtDuration(null)).toBe("—");
    expect(fmtDuration(65)).toBe("1:05");
    expect(fmtDuration(3661)).toBe("1:01:01");
  });

  it("formats upload date (YYYYMMDD)", () => {
    expect(fmtUploadDate("20231005")).toBe("2023-10-05");
    expect(fmtUploadDate(null)).toBe("—");
  });

  it("maps job status to a badge kind", () => {
    expect(statusKind("success")).toBe("ok");
    expect(statusKind("failed")).toBe("err");
    expect(statusKind("running")).toBe("run");
    expect(statusKind("partial_success")).toBe("warn");
    expect(statusKind("queued")).toBe("muted");
  });

  it("maps refresh state to a badge kind", () => {
    expect(stateKind("available")).toBe("ok");
    expect(stateKind("unavailable")).toBe("err");
    expect(stateKind("not_available")).toBe("warn");
    expect(stateKind(null)).toBe("muted");
  });
});
