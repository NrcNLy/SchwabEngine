import React, { useEffect, useState } from 'react';
import { CalendarClock, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { LiquidityPolicy, Weekday } from '../types';
import { updateLiquidityPolicy } from '../services/api';

interface InflowScheduleFormProps {
  policy: LiquidityPolicy | null;
  onSaved: (policy: LiquidityPolicy) => void;
}

const WEEKDAYS: Weekday[] = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY'];

const titleCase = (s: string) => s.charAt(0) + s.slice(1).toLowerCase();

interface Draft {
  enabled: boolean;
  amount: string;
  weekday: Weekday;
  lag: string;
  confidencePct: string;
}

const toDraft = (p: LiquidityPolicy): Draft => ({
  enabled: p.inflow.enabled,
  amount: p.inflow.amount.toFixed(2),
  weekday: p.inflow.weekday,
  lag: String(p.inflow.lag_business_days),
  confidencePct: String(Math.round(p.inflow.confidence * 100)),
});

const inputClass =
  'w-full bg-[#151c2e] border border-gray-700 rounded-lg px-2.5 py-1.5 text-xs font-mono text-gray-200 focus:outline-none focus:border-cyan-500 disabled:opacity-50';

export const InflowScheduleForm: React.FC<InflowScheduleFormProps> = ({ policy, onSaved }) => {
  const [draft, setDraft] = useState<Draft | null>(policy ? toDraft(policy) : null);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; msg: string } | null>(null);

  useEffect(() => {
    if (policy && draft === null) setDraft(toDraft(policy));
  }, [policy, draft]);

  if (!policy || !draft) {
    return <div className="text-xs font-mono text-gray-500">Loading inflow schedule…</div>;
  }

  const amount = Number(draft.amount);
  const lag = Number(draft.lag);
  const confidencePct = Number(draft.confidencePct);
  const validationError =
    !Number.isFinite(amount) || amount < 0
      ? 'Amount must be zero or more.'
      : !Number.isInteger(lag) || lag < 0 || lag > 10
        ? 'Lag must be a whole number of business days (0–10).'
        : !Number.isFinite(confidencePct) || confidencePct < 0 || confidencePct > 100
          ? 'Confidence must be between 0 and 100.'
          : null;

  const dirty = JSON.stringify(draft) !== JSON.stringify(toDraft(policy));

  const update = (patch: Partial<Draft>) => {
    setDraft({ ...draft, ...patch });
    setFeedback(null);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (validationError) return;
    setSaving(true);
    setFeedback(null);
    const next: LiquidityPolicy = {
      ...policy,
      inflow: {
        ...policy.inflow,
        enabled: draft.enabled,
        amount,
        weekday: draft.weekday,
        lag_business_days: lag,
        confidence: confidencePct / 100,
      },
    };
    const res = await updateLiquidityPolicy(next);
    setSaving(false);
    if (res.success) {
      const saved = res.policy ?? next;
      onSaved(saved);
      setDraft(toDraft(saved));
      setFeedback({ ok: true, msg: 'Saved.' });
    } else {
      setFeedback({ ok: false, msg: res.message });
    }
  };

  return (
    <form onSubmit={save} className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold text-gray-300 uppercase tracking-wider font-mono flex items-center">
          <CalendarClock className="w-4 h-4 mr-1.5 text-cyan-500" />
          Inflow schedule
        </h4>
        <label className="flex items-center gap-2 text-[11px] font-mono text-gray-400 cursor-pointer">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => update({ enabled: e.target.checked })}
            className="accent-cyan-500"
          />
          Count expected inflow
        </label>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <label className="space-y-1">
          <span className="text-[10px] font-mono text-gray-500">Weekly amount ($)</span>
          <input
            type="number"
            inputMode="decimal"
            min={0}
            step="0.01"
            value={draft.amount}
            disabled={!draft.enabled}
            onChange={(e) => update({ amount: e.target.value })}
            className={inputClass}
          />
        </label>
        <label className="space-y-1">
          <span className="text-[10px] font-mono text-gray-500">Cadence</span>
          <select
            value={draft.weekday}
            disabled={!draft.enabled}
            onChange={(e) => update({ weekday: e.target.value as Weekday })}
            className={inputClass}
          >
            {WEEKDAYS.map((d) => (
              <option key={d} value={d}>
                {titleCase(d)}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-[10px] font-mono text-gray-500">Lag (business days)</span>
          <input
            type="number"
            inputMode="numeric"
            min={0}
            max={10}
            step={1}
            value={draft.lag}
            disabled={!draft.enabled}
            onChange={(e) => update({ lag: e.target.value })}
            className={inputClass}
          />
        </label>
        <label className="space-y-1">
          <span className="text-[10px] font-mono text-gray-500">Confidence (%)</span>
          <input
            type="number"
            inputMode="numeric"
            min={0}
            max={100}
            step={5}
            value={draft.confidencePct}
            disabled={!draft.enabled}
            onChange={(e) => update({ confidencePct: e.target.value })}
            className={inputClass}
          />
        </label>
      </div>

      <div className="flex items-center justify-between gap-3">
        <div className="text-[11px] font-mono min-h-[1rem]">
          {validationError ? (
            <span className="text-amber-400">{validationError}</span>
          ) : feedback ? (
            <span className={`inline-flex items-center ${feedback.ok ? 'text-emerald-400' : 'text-rose-400'}`}>
              {feedback.ok ? <CheckCircle2 className="w-3.5 h-3.5 mr-1" /> : <XCircle className="w-3.5 h-3.5 mr-1" />}
              {feedback.msg}
            </span>
          ) : null}
        </div>
        <button
          type="submit"
          disabled={saving || !dirty || validationError !== null}
          className="px-3.5 py-1.5 rounded-lg text-xs font-mono font-semibold text-white bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 disabled:hover:bg-cyan-600 transition-colors flex items-center"
        >
          {saving && <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />}
          Save schedule
        </button>
      </div>
    </form>
  );
};
