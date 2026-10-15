import { describe, expect, it } from 'vitest';
import { AveRingBuffer } from '../utils/ringBuffer';
import { AveCompactFrame } from '../types/ave';

describe('AveRingBuffer', () => {
  it('initializes with specified capacity and empty length', () => {
    const buffer = new AveRingBuffer(10);
    expect(buffer.capacity).toBe(10);
    expect(buffer.length).toBe(0);

    const [times, prices, uppers, lowers, suprema] = buffer.getUPlotData();
    expect(times.length).toBe(0);
    expect(prices.length).toBe(0);
    expect(uppers.length).toBe(0);
    expect(lowers.length).toBe(0);
    expect(suprema.length).toBe(0);
  });

  it('pushes single frames and converts ms to seconds for uPlot x-axis', () => {
    const buffer = new AveRingBuffer(5);
    const frame1: AveCompactFrame = [1, 1728950400000, 100.5, 105.0, 95.0, 0.95, 101.0, 2];
    buffer.push(frame1);

    expect(buffer.length).toBe(1);
    const [times, prices, uppers, lowers, suprema] = buffer.getUPlotData();
    expect(times.length).toBe(1);
    expect(times[0]).toBe(1728950400); // ms to sec
    expect(prices[0]).toBe(100.5);
    expect(uppers[0]).toBe(105.0);
    expect(lowers[0]).toBe(95.0);
    expect(suprema[0]).toBe(101.0);
  });

  it('wraps around and retains most recent items when exceeding capacity', () => {
    const buffer = new AveRingBuffer(3);

    const f1: AveCompactFrame = [1, 1000, 10, 12, 8, 1.0, 10, 2];
    const f2: AveCompactFrame = [2, 2000, 11, 13, 9, 0.9, 11, 2];
    const f3: AveCompactFrame = [3, 3000, 12, 14, 10, 0.8, 12, 2];
    const f4: AveCompactFrame = [4, 4000, 13, 15, 11, 0.7, 13, 2];

    buffer.push(f1);
    buffer.push(f2);
    buffer.push(f3);
    expect(buffer.length).toBe(3);

    // Push 4th: should overwrite oldest (f1)
    buffer.push(f4);
    expect(buffer.length).toBe(3);

    const [times, prices] = buffer.getUPlotData();
    expect(times.length).toBe(3);
    // Times should be ordered oldest to newest: 2000ms -> 2s, 3000ms -> 3s, 4000ms -> 4s
    expect(times[0]).toBe(2);
    expect(times[1]).toBe(3);
    expect(times[2]).toBe(4);

    expect(prices[0]).toBe(11);
    expect(prices[1]).toBe(12);
    expect(prices[2]).toBe(13);
  });

  it('supports pushBatch and clear', () => {
    const buffer = new AveRingBuffer(4);
    const batch: AveCompactFrame[] = [
      [1, 1000, 50, 55, 45, 1.0, 50, 2],
      [2, 2000, 51, 56, 46, 0.9, 51, 2],
      [3, 3000, 52, 57, 47, 0.8, 52, 2],
    ];

    buffer.pushBatch(batch);
    expect(buffer.length).toBe(3);

    const [times] = buffer.getUPlotData();
    expect(times.length).toBe(3);
    expect(times[2]).toBe(3);

    buffer.clear();
    expect(buffer.length).toBe(0);
    const [emptyTimes] = buffer.getUPlotData();
    expect(emptyTimes.length).toBe(0);
  });
});
