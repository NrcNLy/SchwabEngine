/**
 * src/utils/ringBuffer.ts
 * =======================
 * Pre-allocated Float64Array ring buffer for zero-GC, low-latency uPlot charting.
 *
 * uPlot expects contiguous arrays in format: [timeArray, priceArray, upperArray, lowerArray, supremumArray].
 * Using pre-allocated typed arrays prevents garbage collection spikes when high-frequency frames arrive.
 */

import { AveCompactFrame } from '../types/ave';

export class AveRingBuffer {
  public capacity: number;
  public length: number = 0;

  // Pre-allocated typed arrays for the 5 series:
  // [0] timestamps in seconds (uPlot requires seconds for time series)
  public times: Float64Array;
  // [1] price
  public prices: Float64Array;
  // [2] upper bound
  public uppers: Float64Array;
  // [3] lower bound
  public lowers: Float64Array;
  // [4] supremum
  public supremums: Float64Array;

  constructor(capacity: number = 500) {
    this.capacity = capacity;
    this.times = new Float64Array(capacity);
    this.prices = new Float64Array(capacity);
    this.uppers = new Float64Array(capacity);
    this.lowers = new Float64Array(capacity);
    this.supremums = new Float64Array(capacity);
  }

  public push(frame: AveCompactFrame): void {
    // Frame format: [seq, epoch_ms, price, upper, lower, lambda, supremum, state_code]
    const epochSec = frame[1] / 1000.0;
    const price = frame[2];
    const upper = frame[3];
    const lower = frame[4];
    const supremum = frame[6];

    if (this.length < this.capacity) {
      const idx = this.length;
      this.times[idx] = epochSec;
      this.prices[idx] = price;
      this.uppers[idx] = upper;
      this.lowers[idx] = lower;
      this.supremums[idx] = supremum;
      this.length++;
    } else {
      // Shift left by 1 (fast typed array copy) and insert at end
      this.times.copyWithin(0, 1);
      this.prices.copyWithin(0, 1);
      this.uppers.copyWithin(0, 1);
      this.lowers.copyWithin(0, 1);
      this.supremums.copyWithin(0, 1);

      const lastIdx = this.capacity - 1;
      this.times[lastIdx] = epochSec;
      this.prices[lastIdx] = price;
      this.uppers[lastIdx] = upper;
      this.lowers[lastIdx] = lower;
      this.supremums[lastIdx] = supremum;
    }
  }

  public pushBatch(frames: AveCompactFrame[]): void {
    for (let i = 0; i < frames.length; i++) {
      this.push(frames[i]);
    }
  }

  public clear(): void {
    this.length = 0;
  }

  /**
   * Returns views (slices) of active typed arrays suitable for uPlot.setData().
   * Subarray views share underlying memory without copying.
   */
  public getUPlotData(): [Float64Array, Float64Array, Float64Array, Float64Array, Float64Array] {
    if (this.length === this.capacity) {
      return [this.times, this.prices, this.uppers, this.lowers, this.supremums];
    }
    return [
      this.times.subarray(0, this.length),
      this.prices.subarray(0, this.length),
      this.uppers.subarray(0, this.length),
      this.lowers.subarray(0, this.length),
      this.supremums.subarray(0, this.length),
    ];
  }
}
