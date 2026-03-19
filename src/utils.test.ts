// =============================================================================
// Tests for utility functions (src/utils.ts)
// =============================================================================

import { describe, it, expect } from "vitest";
import { formatSize, calculateFontSize, GAME_WIDTH, GAME_HEIGHT } from "./utils";

// =============================================================================
// formatSize
// =============================================================================

describe("formatSize", () => {
  it("formats 0 bytes", () => {
    expect(formatSize(0)).toBe("0 B");
  });

  it("formats small byte values", () => {
    expect(formatSize(512)).toBe("512 B");
  });

  it("formats exactly 1023 bytes (just under 1 KB)", () => {
    expect(formatSize(1023)).toBe("1023 B");
  });

  it("formats kilobytes", () => {
    expect(formatSize(1024)).toBe("1.0 KB");
    expect(formatSize(1536)).toBe("1.5 KB");
  });

  it("formats megabytes", () => {
    expect(formatSize(1024 * 1024)).toBe("1.0 MB");
    expect(formatSize(2.5 * 1024 * 1024)).toBe("2.5 MB");
  });

  it("formats large file (170 MB plugin zip)", () => {
    const result = formatSize(170 * 1024 * 1024);
    expect(result).toBe("170.0 MB");
  });
});

// =============================================================================
// calculateFontSize
// =============================================================================

describe("calculateFontSize", () => {
  it("returns value within cropped region bounds (7-16px)", () => {
    const region = { x1: 100, y1: 100, x2: 500, y2: 400 };
    const size = calculateFontSize(100, region);
    expect(size).toBeGreaterThanOrEqual(7);
    expect(size).toBeLessThanOrEqual(16);
  });

  it("returns value within full-screen bounds (12-20px)", () => {
    const size = calculateFontSize(100, null);
    expect(size).toBeGreaterThanOrEqual(12);
    expect(size).toBeLessThanOrEqual(20);
  });

  it("clamps to minimum for cropped region with lots of text", () => {
    const region = { x1: 0, y1: 0, x2: 50, y2: 50 };
    const size = calculateFontSize(10000, region);
    expect(size).toBe(7);
  });

  it("clamps to maximum for cropped region with little text", () => {
    const region = { x1: 0, y1: 0, x2: 1280, y2: 800 };
    const size = calculateFontSize(1, region);
    expect(size).toBe(16);
  });

  it("clamps to minimum for full-screen with lots of text", () => {
    const size = calculateFontSize(100000, null);
    expect(size).toBe(12);
  });

  it("clamps to maximum for full-screen with little text", () => {
    const size = calculateFontSize(1);
    expect(size).toBe(20);
  });

  it("smaller region produces smaller font for same text", () => {
    const small = { x1: 100, y1: 100, x2: 200, y2: 200 };
    const large = { x1: 0, y1: 0, x2: 1280, y2: 800 };
    const sizeSmall = calculateFontSize(50, small);
    const sizeLarge = calculateFontSize(50, large);
    expect(sizeSmall).toBeLessThanOrEqual(sizeLarge);
  });

  it("more text produces smaller font for same area", () => {
    const region = { x1: 0, y1: 0, x2: 800, y2: 400 };
    const sizeShort = calculateFontSize(10, region);
    const sizeLong = calculateFontSize(500, region);
    expect(sizeLong).toBeLessThanOrEqual(sizeShort);
  });

  it("handles undefined region as full-screen", () => {
    const withNull = calculateFontSize(100, null);
    const withUndefined = calculateFontSize(100, undefined);
    expect(withNull).toBe(withUndefined);
  });
});

// =============================================================================
// Constants
// =============================================================================

describe("constants", () => {
  it("GAME_WIDTH is 1280", () => {
    expect(GAME_WIDTH).toBe(1280);
  });

  it("GAME_HEIGHT is 800", () => {
    expect(GAME_HEIGHT).toBe(800);
  });
});
