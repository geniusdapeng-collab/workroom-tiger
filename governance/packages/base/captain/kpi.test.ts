import { describe, expect, it } from "vitest";
import { numericKpiSnapshot } from "./kpi.js";

describe("generic KPI snapshot", () => {
  it("keeps arbitrary finite values including zero, negatives and numeric limits", () => {
    const values = Object.freeze({ quality: 0.62, zero: 0, negative: -1, large: Number.MAX_VALUE, small: Number.MIN_VALUE, "质量.rate/\"'": 0.5 });
    expect(numericKpiSnapshot(values)).toEqual(values);
  });

  it("rejects non-finite numbers and never coerces non-numeric values", () => {
    expect(numericKpiSnapshot({
      nan: NaN, positive: Infinity, negative: -Infinity, numericString: "0.2", empty: "",
      null: null, undefined: undefined, true: true, false: false, object: { value: 0 }, array: [0],
      boxed: new Number(0), bigint: 0n, valid: 0,
    })).toEqual({ valid: 0 });
  });

  it.each([null, undefined, [], [0], "0", 0, false])("invalid container %j produces no metric", (value) => {
    expect(Object.keys(numericKpiSnapshot(value))).toHaveLength(0);
  });

  it("accepts own special names without inherited metrics or prototype mutation", () => {
    const source = Object.create({ inherited: 0.1 });
    Object.defineProperty(source, "__proto__", { value: 0.2, enumerable: true });
    Object.defineProperty(source, "constructor", { value: 0.3, enumerable: true });
    source.quality = 0.4;
    const projected = numericKpiSnapshot(source);
    expect(Object.keys(projected)).toEqual(["__proto__", "constructor", "quality"]);
    expect(projected.__proto__).toBe(0.2);
    expect(projected.constructor).toBe(0.3);
    expect(projected.inherited).toBeUndefined();
    expect(projected.toString).toBeUndefined();
    expect(Object.getPrototypeOf(projected)).toBeNull();
    expect(Object.getPrototypeOf(source)).toEqual({ inherited: 0.1 });
  });
});
