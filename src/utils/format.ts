const usdFmt = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export const usd = (value: number | null | undefined): string =>
  value === null || value === undefined || Number.isNaN(value) ? '—' : usdFmt.format(value);

export const signedUsd = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const body = usdFmt.format(Math.abs(value));
  if (value > 0) return `+${body}`;
  if (value < 0) return `-${body}`;
  return body;
};

export const signedPct = (value: number | null | undefined, digits = 2): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const body = Math.abs(value).toFixed(digits);
  if (value > 0) return `+${body}%`;
  if (value < 0) return `-${body}%`;
  return `${body}%`;
};

export const pnlTone = (value: number | null | undefined): string => {
  if (value === null || value === undefined || value === 0) return 'text-gray-300';
  return value > 0 ? 'text-emerald-400' : 'text-rose-400';
};

export const formatCountdown = (seconds: number | null | undefined): string => {
  if (seconds === null || seconds === undefined || seconds < 0) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
};

/** Raw engine payloads are only rendered in dev builds or when VITE_SHOW_DEBUG=1. */
export const SHOW_DEBUG: boolean =
  import.meta.env.DEV === true || import.meta.env.VITE_SHOW_DEBUG === '1';
