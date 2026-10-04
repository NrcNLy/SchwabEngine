import React from 'react';
import { EnvName } from '../types';

interface EnvToggleProps {
  env: EnvName;
  onChange: (env: EnvName) => void;
  className?: string;
}

const OPTIONS: Array<{ id: EnvName; label: string }> = [
  { id: 'active', label: 'Active' },
  { id: 'sandbox', label: 'Sandbox' },
];

/** Binary Active (Schwab core) / Sandbox (simulated) toggle. There is no "All" view. */
export const EnvToggle: React.FC<EnvToggleProps> = ({ env, onChange, className = '' }) => (
  <div
    role="radiogroup"
    aria-label="Environment"
    className={`shrink-0 inline-flex p-0.5 rounded-lg bg-gray-900 border border-gray-800 ${className}`}
  >
    {OPTIONS.map(({ id, label }) => {
      const selected = env === id;
      const tone =
        id === 'active'
          ? 'bg-emerald-950 text-emerald-300 border-emerald-800/70'
          : 'bg-amber-950 text-amber-300 border-amber-800/70';
      return (
        <button
          key={id}
          type="button"
          role="radio"
          aria-checked={selected}
          onClick={() => onChange(id)}
          className={`px-3 py-1 rounded-md text-xs font-mono font-semibold border transition-colors ${
            selected ? tone : 'text-gray-500 border-transparent hover:text-gray-300'
          }`}
        >
          {label}
        </button>
      );
    })}
  </div>
);
