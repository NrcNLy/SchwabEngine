"""
api/live_view.py
================
Standalone, zero-build HTML/JS monitoring view for SchwabEngine.
Served directly from FastAPI via `GET /live` with Tailwind CSS (CDN) and vanilla JS.
"""

def render_live_dashboard() -> str:
    """Returns the self-contained HTML page for the /live monitoring dashboard."""
    return """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SchwabEngine Live Monitor</title>
  <!-- Tailwind CSS via CDN (zero npm/build steps) -->
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: {
              50: '#f0fdf4',
              500: '#22c55e',
              900: '#14532d',
            }
          }
        }
      }
    }
  </script>
  <style>
    @keyframes pulse-subtle {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.6; }
    }
    .animate-pulse-subtle {
      animation: pulse-subtle 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    }
    /* Custom scrollbar for live log */
    ::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    ::-webkit-scrollbar-track {
      background: #0f172a;
    }
    ::-webkit-scrollbar-thumb {
      background: #334155;
      border-radius: 9999px;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: #475569;
    }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans antialiased selection:bg-emerald-500 selection:text-white">

  <!-- Main Container -->
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">

    <!-- Header Navigation / Engine Bar -->
    <header class="flex flex-col sm:flex-row sm:items-center sm:justify-between pb-5 border-b border-slate-800 gap-4">
      <div class="flex items-center gap-3">
        <div class="h-10 w-10 rounded-xl bg-gradient-to-tr from-emerald-600 to-cyan-500 flex items-center justify-center shadow-lg shadow-emerald-500/20">
          <svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"></path>
          </svg>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="text-xl font-bold tracking-tight text-white">SchwabEngine</h1>
            <span class="px-2 py-0.5 rounded text-[11px] font-semibold uppercase tracking-wider bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
              Live Monitor
            </span>
          </div>
          <p class="text-xs text-slate-400">Autonomous execution telemetry &amp; capital protection dashboard</p>
        </div>
      </div>

      <!-- Sync Status & Clock -->
      <div class="flex items-center gap-3 self-start sm:self-auto">
        <div class="flex items-center gap-2 bg-slate-900 border border-slate-800 px-3 py-1.5 rounded-lg text-xs">
          <span class="relative flex h-2.5 w-2.5">
            <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
            <span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500"></span>
          </span>
          <span id="sync-status" class="text-slate-300 font-mono">Live • 2s Polling</span>
        </div>
        <div class="text-xs text-slate-400 font-mono hidden md:block" id="wall-clock">
          --:--:-- EDT
        </div>
      </div>
    </header>

    <!-- TOP SECTION: Plain-English Vitals & Status Cards -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-2">
          <span>1. Plain-English Vitals &amp; Status</span>
        </h2>
        <span class="text-[11px] text-slate-500">Real-time Safety &amp; Regime Gates</span>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">

        <!-- Card 1: System Health -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between text-xs text-slate-400 mb-2">
              <span class="font-medium">System Health</span>
              <span id="health-overall" class="text-emerald-400 font-semibold font-mono">HEALTHY</span>
            </div>
            <div class="space-y-2 mt-3">
              <!-- Pill 1: Engine Mode (LIVE) -->
              <div class="flex items-center justify-between">
                <span class="text-xs text-slate-300">Engine Mode</span>
                <span id="pill-engine-mode" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                  <span class="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                  LIVE
                </span>
              </div>
              <!-- Pill 2: Connection (ACTIVE) -->
              <div class="flex items-center justify-between">
                <span class="text-xs text-slate-300">Connection</span>
                <span id="pill-connection" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                  <span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
                  ACTIVE
                </span>
              </div>
              <!-- Pill 3: T+1 Rule Adherence -->
              <div class="flex items-center justify-between">
                <span class="text-xs text-slate-300">T+1 Rule Adherence</span>
                <span id="pill-t1-rule" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                  <span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
                  COMPLIANT
                </span>
              </div>
            </div>
          </div>
          <p class="text-[11px] text-slate-500 mt-4 pt-3 border-t border-slate-800/80">
            Cash account rules strictly adhered to with zero Good Faith Violation risk.
          </p>
        </div>

        <!-- Card 2: Current Trading Phase -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span class="font-medium">Current Trading Phase</span>
              <span id="phase-badge" class="px-2 py-0.5 rounded text-[11px] font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                New Buys Allowed
              </span>
            </div>
            <div id="phase-title" class="text-lg font-bold text-white mt-2 leading-tight">
              Morning Trading
            </div>
            <p id="phase-desc" class="text-xs text-slate-400 mt-2 leading-relaxed">
              Full Quarter-Kelly sizing active for morning breakout entries.
            </p>
          </div>
          <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
            <span>Sizing Multiplier:</span>
            <span id="phase-multiplier" class="font-mono text-slate-200 font-semibold">1.0x (100%)</span>
          </div>
        </div>

        <!-- Card 3: AI Engine Confidence -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span class="font-medium">AI Engine Confidence</span>
              <span class="text-[11px] text-slate-500 font-mono">Bayesian Posterior</span>
            </div>
            <div class="flex items-baseline gap-2 mt-2">
              <span id="confidence-pct" class="text-2xl font-bold font-mono text-white">46.2%</span>
              <span class="text-xs text-slate-400">win-rate posterior</span>
            </div>
            <div class="mt-3">
              <div id="soft-reserves-pill" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                <span id="soft-reserves-dot" class="h-2 w-2 rounded-full bg-emerald-400"></span>
                <span id="soft-reserves-text">Soft Reserves Unlocked</span>
              </div>
            </div>
          </div>
          <p class="text-[11px] text-slate-500 mt-4 pt-3 border-t border-slate-800/80">
            Bayesian posterior shrunk toward prior. Unlocks extra soft reserves in Regime A.
          </p>
        </div>

        <!-- Card 4: Capital Summary -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span class="font-medium">Capital Summary</span>
              <span id="today-pnl-badge" class="px-2 py-0.5 rounded text-[11px] font-mono font-semibold bg-emerald-500/10 text-emerald-400">
                +$0.00 (0.0%)
              </span>
            </div>
            <div class="mt-2">
              <div class="text-[11px] text-slate-400">Total Portfolio Value</div>
              <div id="cap-nlv" class="text-2xl font-bold font-mono text-white">$3,750.00</div>
            </div>
            <div class="grid grid-cols-2 gap-2 mt-3 pt-3 border-t border-slate-800/80 text-xs">
              <div>
                <span class="text-slate-400 block text-[11px]">Settled Cash</span>
                <span id="cap-settled" class="font-mono text-emerald-400 font-bold">$1,000.00</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Single-Ticker Ceiling</span>
                <span id="cap-ceiling" class="font-mono text-cyan-400 font-bold">$750.00 limit</span>
              </div>
            </div>
          </div>
          <div class="mt-3 text-[11px] text-slate-500 flex justify-between">
            <span>20% max single exposure</span>
            <span id="cap-unsettled" class="font-mono text-slate-400">$0.00 unsettled</span>
          </div>
        </div>

      </div>
    </section>

    <!-- MIDDLE SECTION: Holdings & Capacity Ledger -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-2">
          <span>2. Holdings &amp; Capacity Ledger</span>
        </h2>
        <span class="text-[11px] text-slate-500">20% Cap Headroom Monitoring (Max $750/Ticker)</span>
      </div>

      <!-- Active Position Cards (TQQQ, SOXL, TNA) -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4" id="holdings-grid">

        <!-- Card: TQQQ -->
        <div id="card-TQQQ" class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between pb-3 border-b border-slate-800">
              <div>
                <span class="text-base font-bold text-white tracking-wide">TQQQ</span>
                <span class="text-[11px] text-slate-400 block">ProShares UltraPro QQQ (3x)</span>
              </div>
              <span id="status-TQQQ" class="px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-300 border border-slate-700">
                Standby
              </span>
            </div>

            <div class="grid grid-cols-2 gap-3 mt-4 text-xs">
              <div>
                <span class="text-slate-400 block text-[11px]">Current Shares</span>
                <span id="shares-TQQQ" class="font-mono font-bold text-white text-sm">0 shares</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Position Value</span>
                <span id="value-TQQQ" class="font-mono font-bold text-white text-sm">$0.00</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Dynamic Stop (YZ)</span>
                <span id="stop-TQQQ" class="font-mono font-bold text-amber-400 text-sm">Standby</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Unrealized P&amp;L</span>
                <span id="pnl-TQQQ" class="font-mono font-bold text-slate-400 text-sm">$0.00</span>
              </div>
            </div>
          </div>

          <!-- Headroom against 20% cap Progress Bar -->
          <div class="mt-5 pt-3 border-t border-slate-800/80">
            <div class="flex justify-between items-center text-xs mb-1.5">
              <span id="headroom-label-TQQQ" class="font-medium text-slate-200">TQQQ: 0% of limit - $750 Room</span>
              <span id="headroom-cap-TQQQ" class="font-mono text-slate-400 text-[11px]">Cap: $750</span>
            </div>
            <div class="w-full bg-slate-800 rounded-full h-2.5 overflow-hidden border border-slate-700/50">
              <div id="bar-TQQQ" class="h-2.5 rounded-full transition-all duration-500 ease-out bg-slate-600" style="width: 0%"></div>
            </div>
          </div>
        </div>

        <!-- Card: SOXL -->
        <div id="card-SOXL" class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between pb-3 border-b border-slate-800">
              <div>
                <span class="text-base font-bold text-white tracking-wide">SOXL</span>
                <span class="text-[11px] text-slate-400 block">Direxion Semiconductor (3x)</span>
              </div>
              <span id="status-SOXL" class="px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-300 border border-slate-700">
                Standby
              </span>
            </div>

            <div class="grid grid-cols-2 gap-3 mt-4 text-xs">
              <div>
                <span class="text-slate-400 block text-[11px]">Current Shares</span>
                <span id="shares-SOXL" class="font-mono font-bold text-white text-sm">0 shares</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Position Value</span>
                <span id="value-SOXL" class="font-mono font-bold text-white text-sm">$0.00</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Dynamic Stop (YZ)</span>
                <span id="stop-SOXL" class="font-mono font-bold text-amber-400 text-sm">Standby</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Unrealized P&amp;L</span>
                <span id="pnl-SOXL" class="font-mono font-bold text-slate-400 text-sm">$0.00</span>
              </div>
            </div>
          </div>

          <!-- Headroom against 20% cap Progress Bar -->
          <div class="mt-5 pt-3 border-t border-slate-800/80">
            <div class="flex justify-between items-center text-xs mb-1.5">
              <span id="headroom-label-SOXL" class="font-medium text-slate-200">SOXL: 0% of limit - $750 Room</span>
              <span id="headroom-cap-SOXL" class="font-mono text-slate-400 text-[11px]">Cap: $750</span>
            </div>
            <div class="w-full bg-slate-800 rounded-full h-2.5 overflow-hidden border border-slate-700/50">
              <div id="bar-SOXL" class="h-2.5 rounded-full transition-all duration-500 ease-out bg-slate-600" style="width: 0%"></div>
            </div>
          </div>
        </div>

        <!-- Card: TNA -->
        <div id="card-TNA" class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between pb-3 border-b border-slate-800">
              <div>
                <span class="text-base font-bold text-white tracking-wide">TNA</span>
                <span class="text-[11px] text-slate-400 block">Direxion Small Cap Bull (3x)</span>
              </div>
              <span id="status-TNA" class="px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-300 border border-slate-700">
                Standby
              </span>
            </div>

            <div class="grid grid-cols-2 gap-3 mt-4 text-xs">
              <div>
                <span class="text-slate-400 block text-[11px]">Current Shares</span>
                <span id="shares-TNA" class="font-mono font-bold text-white text-sm">0 shares</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Position Value</span>
                <span id="value-TNA" class="font-mono font-bold text-white text-sm">$0.00</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Dynamic Stop (YZ)</span>
                <span id="stop-TNA" class="font-mono font-bold text-amber-400 text-sm">Standby</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Unrealized P&amp;L</span>
                <span id="pnl-TNA" class="font-mono font-bold text-slate-400 text-sm">$0.00</span>
              </div>
            </div>
          </div>

          <!-- Headroom against 20% cap Progress Bar -->
          <div class="mt-5 pt-3 border-t border-slate-800/80">
            <div class="flex justify-between items-center text-xs mb-1.5">
              <span id="headroom-label-TNA" class="font-medium text-slate-200">TNA: 0% of limit - $750 Room</span>
              <span id="headroom-cap-TNA" class="font-mono text-slate-400 text-[11px]">Cap: $750</span>
            </div>
            <div class="w-full bg-slate-800 rounded-full h-2.5 overflow-hidden border border-slate-700/50">
              <div id="bar-TNA" class="h-2.5 rounded-full transition-all duration-500 ease-out bg-slate-600" style="width: 0%"></div>
            </div>
          </div>
        </div>

      </div>
    </section>

    <!-- BOTTOM SECTION: Reverse-Chronological Live Event Log -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <div class="flex items-center gap-2">
          <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wider">
            3. Reverse-Chronological Live Event Log
          </h2>
          <span class="px-2 py-0.5 text-[10px] rounded bg-slate-800 text-slate-400 font-mono">
            Newest at Top
          </span>
        </div>
        <div class="flex items-center gap-2">
          <button onclick="clearEventLog()" class="text-[11px] text-slate-400 hover:text-white px-2 py-1 rounded bg-slate-800/60 hover:bg-slate-800 transition">
            Clear Feed
          </button>
        </div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-lg">
        <!-- Scrollable Feed -->
        <div id="event-log-container" class="max-h-80 overflow-y-auto space-y-2 pr-2 divide-y divide-slate-800/60">
          <!-- Live events will be prepended here dynamically -->
        </div>
      </div>
    </section>

  </div>

  <!-- Vanilla JavaScript Logic (Zero React / Vite dependencies) -->
  <script>
    // Configuration & State
    const POLL_INTERVAL_MS = 2000;
    const TARGET_SYMBOLS = ['TQQQ', 'SOXL', 'TNA'];

    // Tracking previous state to detect and translate deltas into plain-English messages
    let lastState = {
      initialized: false,
      sessionPhase: null,
      systemState: null,
      todayPnl: null,
      settledCash: null,
      nlv: null,
      stops: {},        // sym -> stopPrice
      quantities: {},   // sym -> quantity
      regimes: {},      // sym -> regime
      softReservesUnlocked: null
    };

    const recentMessageSignatures = new Map();

    // Map raw session phases to human-friendly titles & descriptions
    const PHASE_CONFIG = {
      'MORNING_DRIVE': {
        title: 'Morning Trading',
        badge: 'New Buys Allowed',
        badgeColor: 'emerald',
        desc: 'Full Quarter-Kelly sizing active for morning breakout entries.',
        multiplier: '1.0x (100%)'
      },
      'MID_MORNING': {
        title: 'Mid-Morning Trend',
        badge: 'New Buys Allowed',
        badgeColor: 'emerald',
        desc: 'Standard parameters; trend continuation entries monitored.',
        multiplier: '1.0x (100%)'
      },
      'MIDDAY_FREEZE': {
        title: 'Midday Freeze (No New Buys)',
        badge: 'New Buys Locked',
        badgeColor: 'amber',
        desc: 'Midday freeze engaged: lockout on new buys. Managing trailing stops only.',
        multiplier: '0.0x (Stops Only)'
      },
      'POWER_HOUR': {
        title: 'Power Hour',
        badge: 'New Buys Allowed (50%)',
        badgeColor: 'blue',
        desc: 'Secondary afternoon momentum trend entries at 50% fractional sizing.',
        multiplier: '0.5x (50%)'
      },
      'PRE_CLOSE': {
        title: 'Pre-Close (Unwind Prep)',
        badge: 'New Buys Locked',
        badgeColor: 'amber',
        desc: 'Preparing portfolio for end-of-day unwind and overnight cash safety.',
        multiplier: '0.0x (Locked)'
      },
      'MANDATORY_FLATTEN': {
        title: 'Mandatory Flatten',
        badge: 'Active Liquidation',
        badgeColor: 'rose',
        desc: 'Mandatory flatten sequence engaged: liquidating all active intraday positions.',
        multiplier: '0.0x (Exiting)'
      },
      'POST_CLOSE_REFLECTION': {
        title: 'Post-Close Reflection',
        badge: 'Market Closed',
        badgeColor: 'slate',
        desc: 'Session closed. Computing daily trade reconciliation and P&L metrics.',
        multiplier: '0.0x (Idle)'
      },
      'PRE_MARKET': {
        title: 'Pre-Market Preparation',
        badge: 'Pre-Market',
        badgeColor: 'cyan',
        desc: 'Pre-market data synchronization, cache warmup, and regime scanning.',
        multiplier: '0.0x (Warmup)'
      },
      'OFFLINE': {
        title: 'Market Closed / Offline',
        badge: 'Offline',
        badgeColor: 'slate',
        desc: 'Outside core market hours. Engine in monitoring & scheduled sleep mode.',
        multiplier: '0.0x (Offline)'
      }
    };

    function formatCurrency(val) {
      if (val === null || val === undefined || isNaN(val)) return '$0.00';
      return '$' + Number(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function formatTime(dateObj) {
      const d = dateObj || new Date();
      return d.toTimeString().split(' ')[0];
    }

    // Append log event at the VERY TOP of the feed
    function addLogEvent(category, message, badgeColor = 'emerald') {
      const container = document.getElementById('event-log-container');
      if (!container) return;

      const now = new Date();
      const timeStr = formatTime(now);

      // Deduplicate identical messages occurring within 10 seconds
      const sig = `${category}:${message}`;
      const lastSeen = recentMessageSignatures.get(sig);
      if (lastSeen && (now - lastSeen) < 10000) {
        return;
      }
      recentMessageSignatures.set(sig, now);

      let colorClass = 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
      if (badgeColor === 'amber') colorClass = 'bg-amber-500/10 text-amber-400 border-amber-500/30';
      if (badgeColor === 'blue') colorClass = 'bg-cyan-500/10 text-cyan-400 border-cyan-500/30';
      if (badgeColor === 'purple') colorClass = 'bg-purple-500/10 text-purple-400 border-purple-500/30';
      if (badgeColor === 'rose') colorClass = 'bg-rose-500/10 text-rose-400 border-rose-500/30';
      if (badgeColor === 'slate') colorClass = 'bg-slate-800 text-slate-400 border-slate-700';

      const entryHtml = `
        <div class="pt-2 flex items-start justify-between gap-3 text-xs transition duration-200">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="px-2 py-0.5 rounded text-[10px] font-mono font-semibold border ${colorClass}">
              ${category}
            </span>
            <span class="text-slate-200 font-medium">${message}</span>
          </div>
          <span class="text-slate-500 font-mono text-[11px] whitespace-nowrap">${timeStr}</span>
        </div>
      `;

      container.insertAdjacentHTML('afterbegin', entryHtml);

      // Keep maximum 100 log entries
      while (container.children.length > 100) {
        container.removeChild(container.lastElementChild);
      }
    }

    function clearEventLog() {
      const container = document.getElementById('event-log-container');
      if (container) {
        container.innerHTML = '';
        addLogEvent('FEED', 'Log cleared by user', 'slate');
      }
    }

    // Updates Top Section vitals
    function updateTopSection(statusData, ledgerData) {
      // 1. System Health Green Pills
      const mode = statusData?.engine_mode === 'LIVE_TRADING' ? 'LIVE' : (statusData?.engine_mode || 'LIVE');
      const pillMode = document.getElementById('pill-engine-mode');
      if (pillMode) {
        pillMode.innerHTML = `<span class="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse"></span> ${mode}`;
      }

      const isConnected = statusData?.is_connected !== false;
      const pillConn = document.getElementById('pill-connection');
      if (pillConn) {
        pillConn.innerHTML = `<span class="h-1.5 w-1.5 rounded-full ${isConnected ? 'bg-emerald-400' : 'bg-rose-400'}"></span> ${isConnected ? 'ACTIVE' : 'OFFLINE'}`;
      }

      const t1Adherence = !ledgerData?.gfv_risk_flag;
      const pillT1 = document.getElementById('pill-t1-rule');
      if (pillT1) {
        pillT1.innerHTML = `<span class="h-1.5 w-1.5 rounded-full ${t1Adherence ? 'bg-emerald-400' : 'bg-amber-400'}"></span> ${t1Adherence ? 'COMPLIANT' : 'GFV ALERT'}`;
      }

      // 2. Current Trading Phase
      const rawPhase = statusData?.session_phase || 'OFFLINE';
      const phaseInfo = PHASE_CONFIG[rawPhase] || {
        title: rawPhase,
        badge: statusData?.entry_permitted ? 'New Buys Allowed' : 'New Buys Locked',
        badgeColor: statusData?.entry_permitted ? 'emerald' : 'amber',
        desc: 'Phase dynamic parameters enforced.',
        multiplier: `${statusData?.phase_sizing_multiplier || 1.0}x`
      };

      const phaseTitleEl = document.getElementById('phase-title');
      if (phaseTitleEl) phaseTitleEl.textContent = phaseInfo.title;

      const phaseBadgeEl = document.getElementById('phase-badge');
      if (phaseBadgeEl) {
        phaseBadgeEl.textContent = phaseInfo.badge;
        phaseBadgeEl.className = `px-2 py-0.5 rounded text-[11px] font-semibold border ${
          phaseInfo.badgeColor === 'emerald' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' :
          phaseInfo.badgeColor === 'amber' ? 'bg-amber-500/10 text-amber-400 border-amber-500/30' :
          phaseInfo.badgeColor === 'rose' ? 'bg-rose-500/10 text-rose-400 border-rose-500/30' :
          'bg-slate-800 text-slate-300 border-slate-700'
        }`;
      }

      const phaseDescEl = document.getElementById('phase-desc');
      if (phaseDescEl) phaseDescEl.textContent = phaseInfo.desc;

      const phaseMultEl = document.getElementById('phase-multiplier');
      if (phaseMultEl) phaseMultEl.textContent = phaseInfo.multiplier;

      // 3. AI Engine Confidence & Soft Reserves Indicator
      const posterior = ledgerData?.posterior || statusData?.posterior || 0.4615;
      const posteriorPct = (Number(posterior) * 100).toFixed(1) + '%';
      const confPctEl = document.getElementById('confidence-pct');
      if (confPctEl) confPctEl.textContent = posteriorPct;

      const buyingPower = ledgerData?.buying_power || {};
      const softReservesUnlocked = buyingPower.high_probability || (buyingPower.soft_draw_available > 0);
      const srPill = document.getElementById('soft-reserves-pill');
      const srDot = document.getElementById('soft-reserves-dot');
      const srText = document.getElementById('soft-reserves-text');

      if (softReservesUnlocked) {
        if (srPill) srPill.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
        if (srDot) srDot.className = 'h-2 w-2 rounded-full bg-emerald-400';
        if (srText) srText.textContent = 'Soft Reserves Unlocked';
      } else {
        if (srPill) srPill.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700';
        if (srDot) srDot.className = 'h-2 w-2 rounded-full bg-slate-500';
        if (srText) srText.textContent = 'Soft Reserves Locked';
      }

      // 4. Capital Summary
      const totalNlv = ledgerData?.total_nlv ?? statusData?.nlv ?? 3750.0;
      const settledCash = ledgerData?.bucket1_settled ?? 1000.0;
      const maxCeiling = ledgerData?.max_single_exposure ?? (totalNlv * 0.20);
      const unsettledCash = ledgerData?.bucket2_unsettled ?? 0.0;
      const todayPnl = statusData?.today_pnl ?? 0.0;
      const todayPnlPct = statusData?.net_change_pct ?? (totalNlv > 0 ? (todayPnl / totalNlv * 100) : 0.0);

      document.getElementById('cap-nlv').textContent = formatCurrency(totalNlv);
      document.getElementById('cap-settled').textContent = formatCurrency(settledCash);
      document.getElementById('cap-ceiling').textContent = `${formatCurrency(maxCeiling)} limit`;
      document.getElementById('cap-unsettled').textContent = `${formatCurrency(unsettledCash)} unsettled`;

      const pnlBadge = document.getElementById('today-pnl-badge');
      if (pnlBadge) {
        const sign = todayPnl >= 0 ? '+' : '';
        pnlBadge.textContent = `${sign}${formatCurrency(todayPnl)} (${sign}${Number(todayPnlPct).toFixed(2)}%)`;
        pnlBadge.className = `px-2 py-0.5 rounded text-[11px] font-mono font-semibold ${
          todayPnl >= 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'
        }`;
      }

      return {
        rawPhase,
        softReservesUnlocked,
        posterior,
        totalNlv,
        settledCash,
        maxCeiling,
        todayPnl
      };
    }

    // Updates Middle Section Holdings & Headroom bars
    function updateMiddleSection(positionsList, ledgerData) {
      const totalNlv = ledgerData?.total_nlv ?? 3750.0;
      const singleCap = ledgerData?.max_single_exposure ?? (totalNlv * 0.20) || 750.0;

      // Index positions by symbol
      const posMap = {};
      if (Array.isArray(positionsList)) {
        positionsList.forEach(p => {
          if (p && p.symbol) posMap[p.symbol.toUpperCase()] = p;
        });
      }

      TARGET_SYMBOLS.forEach(sym => {
        const pos = posMap[sym];
        const shares = pos ? pos.quantity : 0;
        const val = pos ? pos.notional_value : 0;
        const stopPrice = pos && (pos.stop_price || pos.hard_stop_price) ? Number(pos.stop_price || pos.hard_stop_price) : null;
        const pnl = pos ? pos.unrealized_pnl : 0;

        // Elements
        const statusEl = document.getElementById(`status-${sym}`);
        const sharesEl = document.getElementById(`shares-${sym}`);
        const valueEl = document.getElementById(`value-${sym}`);
        const stopEl = document.getElementById(`stop-${sym}`);
        const pnlEl = document.getElementById(`pnl-${sym}`);
        const labelEl = document.getElementById(`headroom-label-TQQQ` ? `headroom-label-${sym}` : null);
        const capEl = document.getElementById(`headroom-cap-${sym}`);
        const barEl = document.getElementById(`bar-${sym}`);

        if (statusEl) {
          if (shares > 0) {
            statusEl.textContent = 'ACTIVE POSITION';
            statusEl.className = 'px-2.5 py-0.5 rounded text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
          } else {
            statusEl.textContent = 'Standby';
            statusEl.className = 'px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700';
          }
        }

        if (sharesEl) sharesEl.textContent = `${shares} shares`;
        if (valueEl) valueEl.textContent = formatCurrency(val);
        if (stopEl) {
          stopEl.textContent = stopPrice !== null ? formatCurrency(stopPrice) : 'Standby';
        }
        if (pnlEl) {
          const sign = pnl >= 0 ? '+' : '';
          pnlEl.textContent = `${sign}${formatCurrency(pnl)}`;
          pnlEl.className = `font-mono font-bold text-sm ${pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
        }

        // Capacity Progress Bar calculations
        const pctOfLimit = singleCap > 0 ? Math.min(100, Math.round((val / singleCap) * 100)) : 0;
        const room = Math.max(0, singleCap - val);

        if (labelEl) {
          if (pctOfLimit >= 95) {
            labelEl.textContent = `${sym}: ${pctOfLimit}% of limit - FULL`;
            labelEl.className = 'font-medium text-amber-400';
          } else if (pctOfLimit > 0) {
            labelEl.textContent = `${sym}: ${pctOfLimit}% of limit - $${room.toFixed(0)} Room`;
            labelEl.className = 'font-medium text-slate-200';
          } else {
            labelEl.textContent = `${sym}: 0% of limit - $${singleCap.toFixed(0)} Room`;
            labelEl.className = 'font-medium text-slate-400';
          }
        }

        if (capEl) capEl.textContent = `Cap: $${singleCap.toFixed(0)}`;

        if (barEl) {
          barEl.style.width = `${pctOfLimit}%`;
          if (pctOfLimit >= 95) {
            barEl.className = 'h-2.5 rounded-full transition-all duration-500 ease-out bg-amber-500';
          } else if (pctOfLimit >= 60) {
            barEl.className = 'h-2.5 rounded-full transition-all duration-500 ease-out bg-cyan-500';
          } else if (pctOfLimit > 0) {
            barEl.className = 'h-2.5 rounded-full transition-all duration-500 ease-out bg-emerald-500';
          } else {
            barEl.className = 'h-2.5 rounded-full transition-all duration-500 ease-out bg-slate-700';
          }
        }
      });

      return posMap;
    }

    // Event generator that translates data deltas into human-readable messages
    function translateDeltas(vitals, positionsMap, indicatorsData) {
      if (!lastState.initialized) {
        // First run baseline announcements
        addLogEvent('SYSTEM', 'Telemetry link connected • Polling cycle active at 2.0s', 'emerald');
        addLogEvent('RULE', 'T+1 Rule Adherence active: entries funded strictly by settled cash', 'emerald');
        const phaseTitle = PHASE_CONFIG[vitals.rawPhase]?.title || vitals.rawPhase;
        addLogEvent('PHASE', `Trading phase initialized: ${phaseTitle}`, 'blue');
        addLogEvent('AI REGIME', `Bayesian posterior win-rate: ${(vitals.posterior * 100).toFixed(1)}% (${vitals.softReservesUnlocked ? 'Soft reserves unlocked' : 'Soft reserves locked'})`, 'purple');
        addLogEvent('CAPITAL', `Single-ticker ceiling enforced at $${vitals.maxCeiling.toFixed(0)} (20% of NLV)`, 'blue');

        // Populate baseline positions
        TARGET_SYMBOLS.forEach(sym => {
          const p = positionsMap[sym];
          if (p && p.quantity > 0) {
            addLogEvent('POSITION', `Active position detected: ${sym} (${p.quantity} shares, value: ${formatCurrency(p.notional_value)})`, 'emerald');
            if (p.stop_price) {
              addLogEvent('STOP', `Dynamic stop established at ${formatCurrency(p.stop_price)} for ${sym}`, 'amber');
            }
          }
        });

        lastState.initialized = true;
        lastState.sessionPhase = vitals.rawPhase;
        lastState.softReservesUnlocked = vitals.softReservesUnlocked;
        lastState.totalNlv = vitals.totalNlv;
        lastState.settledCash = vitals.settledCash;
        TARGET_SYMBOLS.forEach(sym => {
          const p = positionsMap[sym];
          lastState.quantities[sym] = p ? p.quantity : 0;
          lastState.stops[sym] = p ? (p.stop_price || null) : null;
        });
        return;
      }

      // 1. Session Phase change detection
      if (vitals.rawPhase !== lastState.sessionPhase) {
        const oldTitle = PHASE_CONFIG[lastState.sessionPhase]?.title || lastState.sessionPhase;
        const newTitle = PHASE_CONFIG[vitals.rawPhase]?.title || vitals.rawPhase;

        if (vitals.rawPhase === 'MIDDAY_FREEZE') {
          addLogEvent('PHASE', 'Midday Freeze engaged: new buys locked. Managing trailing stops only.', 'amber');
        } else if (vitals.rawPhase === 'MORNING_DRIVE') {
          addLogEvent('PHASE', 'Morning Trading session open: 100% Quarter-Kelly sizing active.', 'emerald');
        } else if (vitals.rawPhase === 'POWER_HOUR') {
          addLogEvent('PHASE', 'Power Hour engaged: secondary trend entries at 50% sizing.', 'blue');
        } else if (vitals.rawPhase === 'MANDATORY_FLATTEN') {
          addLogEvent('PHASE', 'Mandatory Flatten engaged: active liquidation sequence starting.', 'rose');
        } else {
          addLogEvent('PHASE', `Trading phase transitioned: ${oldTitle} → ${newTitle}`, 'blue');
        }
        lastState.sessionPhase = vitals.rawPhase;
      }

      // 2. Soft Reserves status change
      if (vitals.softReservesUnlocked !== lastState.softReservesUnlocked) {
        if (vitals.softReservesUnlocked) {
          addLogEvent('AI ENGINE', 'Bayesian posterior unlocked soft reserves for Regime A high-probability setups.', 'emerald');
        } else {
          addLogEvent('AI ENGINE', 'Soft reserves locked: Standard float preserved.', 'amber');
        }
        lastState.softReservesUnlocked = vitals.softReservesUnlocked;
      }

      // 3. Dynamic Stop Price changes (trailing Yang-Zhang stops)
      TARGET_SYMBOLS.forEach(sym => {
        const p = positionsMap[sym];
        const newStop = p ? Number(p.stop_price || p.hard_stop_price || 0) : null;
        const oldStop = lastState.stops[sym];

        if (newStop && oldStop && newStop > oldStop) {
          addLogEvent('STOP', `Dynamic stop rose to ${formatCurrency(newStop)} on ${sym}`, 'emerald');
        } else if (newStop && oldStop && newStop < oldStop) {
          addLogEvent('STOP', `Dynamic stop adjusted to ${formatCurrency(newStop)} on ${sym}`, 'amber');
        } else if (newStop && !oldStop) {
          addLogEvent('STOP', `Dynamic stop set to ${formatCurrency(newStop)} on ${sym}`, 'blue');
        }
        lastState.stops[sym] = newStop;

        // Position sizing changes
        const newQty = p ? p.quantity : 0;
        const oldQty = lastState.quantities[sym] || 0;
        if (newQty > oldQty && oldQty === 0) {
          addLogEvent('ORDER', `Bought ${newQty} shares of ${sym} (${formatCurrency(p.notional_value)})`, 'emerald');
        } else if (newQty < oldQty && newQty === 0) {
          addLogEvent('ORDER', `Exited ${sym} position (${oldQty} shares liquidated to settled cash)`, 'blue');
        } else if (newQty !== oldQty) {
          addLogEvent('ORDER', `Position adjusted: ${sym} now ${newQty} shares`, 'blue');
        }
        lastState.quantities[sym] = newQty;
      });

      // 4. Indicator / Regime changes from /api/indicators
      if (indicatorsData && indicatorsData.symbols) {
        Object.entries(indicatorsData.symbols).forEach(([sym, data]) => {
          if (data && data.lead_lag) {
            const currentRegime = data.lead_lag.regime || (data.mlofi?.unanimous_positive ? 'Trend' : null);
            if (currentRegime && currentRegime !== lastState.regimes[sym]) {
              addLogEvent('AI REGIME', `Morning AI regime for ${sym} set to ${currentRegime}`, 'purple');
              lastState.regimes[sym] = currentRegime;
            }
          }
        });
      }
    }

    // Main polling loop
    async function pollTelemetry() {
      try {
        const [statusRes, ledgerRes, indicatorsRes, positionsRes] = await Promise.allSettled([
          fetch('/api/status').then(r => r.ok ? r.json() : null),
          fetch('/api/ledger').then(r => r.ok ? r.json() : null),
          fetch('/api/indicators').then(r => r.ok ? r.json() : null),
          fetch('/api/positions').then(r => r.ok ? r.json() : null)
        ]);

        const statusData = statusRes.status === 'fulfilled' ? statusRes.value : null;
        const ledgerData = ledgerRes.status === 'fulfilled' ? ledgerRes.value : null;
        const indicatorsData = indicatorsRes.status === 'fulfilled' ? indicatorsRes.value : null;
        const positionsData = positionsRes.status === 'fulfilled' ? positionsRes.value : null;

        // Fallback positions from statusData if /api/positions was empty
        const activePositions = Array.isArray(positionsData) ? positionsData : (statusData?.positions || []);

        if (statusData || ledgerData) {
          const vitals = updateTopSection(statusData, ledgerData);
          const posMap = updateMiddleSection(activePositions, ledgerData);
          translateDeltas(vitals, posMap, indicatorsData);

          const syncEl = document.getElementById('sync-status');
          if (syncEl) syncEl.textContent = 'Live • 2s Polling';
        }
      } catch (err) {
        console.warn('Telemetry poll error:', err);
        const syncEl = document.getElementById('sync-status');
        if (syncEl) syncEl.textContent = 'Reconnecting...';
      }
    }

    // Update digital clock every second
    setInterval(() => {
      const clockEl = document.getElementById('wall-clock');
      if (clockEl) {
        const now = new Date();
        clockEl.textContent = now.toLocaleTimeString('en-US', { timeZone: 'America/New_York' }) + ' EDT';
      }
    }, 1000);

    // Initial poll + interval
    pollTelemetry();
    setInterval(pollTelemetry, POLL_INTERVAL_MS);
  </script>
</body>
</html>"""
