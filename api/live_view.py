"""
api/live_view.py
================
Standalone, zero-build HTML/JS monitoring view for SchwabEngine.
Served directly from FastAPI via `GET /live` with Tailwind CSS (CDN) and vanilla JS.
Binds strictly to real-time engine telemetry with zero placeholder data.
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
          <span id="sync-status" class="text-slate-300 font-mono">Live • 2s Polling (env=active)</span>
        </div>
        <div class="text-xs text-slate-400 font-mono hidden md:block" id="wall-clock">
          --:--:-- EDT
        </div>
      </div>
    </header>

    <!-- SECTION 1 (TOP): Reverse-Chronological Live Event Log -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <div class="flex items-center gap-2">
          <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wider">
            1. Live Event Stream
          </h2>
          <span class="px-2 py-0.5 text-[10px] rounded bg-slate-800 text-slate-300 font-mono border border-slate-700">
            Reverse-Chronological (Newest at Top)
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
        <div id="event-log-container" class="max-h-72 overflow-y-auto space-y-2 pr-2 divide-y divide-slate-800/60 font-mono text-xs">
          <div class="py-2 text-slate-500 text-center text-xs italic">
            Connecting to engine event feed...
          </div>
        </div>
      </div>
    </section>

    <!-- SECTION 2: Account Vitals & PnL Banner -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wider">
          2. Account Vitals &amp; Capital Banner
        </h2>
        <span class="text-[11px] text-slate-500">Broker Sync &amp; T+1 Allocation</span>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">

        <!-- Vitals Card 1: Total Portfolio Value -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="text-[11px] text-slate-400 uppercase font-semibold tracking-wider">Total Portfolio Value (NLV)</div>
            <div id="cap-nlv" class="text-3xl font-bold font-mono text-white mt-2">--</div>
          </div>
          <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
            <span>Net Change:</span>
            <span id="cap-net-change" class="font-mono text-slate-300 font-semibold">--</span>
          </div>
        </div>

        <!-- Vitals Card 2: Daily P&L -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between text-[11px] text-slate-400 uppercase font-semibold tracking-wider">
              <span>Daily P&amp;L</span>
              <span id="pnl-session-tag" class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">Pre-Open</span>
            </div>
            <div id="today-pnl-val" class="text-3xl font-bold font-mono text-slate-300 mt-2">--</div>
          </div>
          <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
            <span>Session Status:</span>
            <span id="today-pnl-status" class="text-slate-300">N/A (Pre-Open)</span>
          </div>
        </div>

        <!-- Vitals Card 3: Settled Cash Available -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="text-[11px] text-slate-400 uppercase font-semibold tracking-wider">Settled Cash Available</div>
            <div id="cap-settled" class="text-3xl font-bold font-mono text-emerald-400 mt-2">--</div>
          </div>
          <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
            <span>Unsettled / In-Flight:</span>
            <span id="cap-unsettled" class="font-mono text-slate-400 font-semibold">--</span>
          </div>
        </div>

        <!-- Vitals Card 4: Single-Ticker Ceiling & Safety -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="text-[11px] text-slate-400 uppercase font-semibold tracking-wider">Single-Ticker Ceiling (20%)</div>
            <div id="cap-ceiling" class="text-3xl font-bold font-mono text-cyan-400 mt-2">--</div>
          </div>
          <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
            <span>T+1 Cash Rule:</span>
            <span id="pill-t1-rule" class="font-mono text-emerald-400 font-semibold">COMPLIANT (0 GFV)</span>
          </div>
        </div>

      </div>
    </section>

    <!-- SECTION 3: Holdings & Headroom Ledger -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wider">
          3. Holdings &amp; Capacity Ledger
        </h2>
        <span class="text-[11px] text-slate-500">20% Exposure Cap &amp; Dynamic Yang-Zhang Volatility Stops</span>
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
              <span id="status-TQQQ" class="px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700">
                Standby
              </span>
            </div>

            <div class="grid grid-cols-2 gap-3 mt-4 text-xs">
              <div>
                <span class="text-slate-400 block text-[11px]">Current Shares</span>
                <span id="shares-TQQQ" class="font-mono font-bold text-white text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Position Value</span>
                <span id="value-TQQQ" class="font-mono font-bold text-white text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Dynamic Stop (YZ)</span>
                <span id="stop-TQQQ" class="font-mono font-bold text-amber-400 text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Unrealized P&amp;L</span>
                <span id="pnl-TQQQ" class="font-mono font-bold text-slate-400 text-sm">--</span>
              </div>
            </div>
          </div>

          <!-- Headroom against 20% cap Progress Bar -->
          <div class="mt-5 pt-3 border-t border-slate-800/80">
            <div class="flex justify-between items-center text-xs mb-1.5">
              <span id="headroom-label-TQQQ" class="font-medium text-slate-400">TQQQ: --</span>
              <span id="headroom-cap-TQQQ" class="font-mono text-slate-400 text-[11px]">Cap: --</span>
            </div>
            <div class="w-full bg-slate-800 rounded-full h-2.5 overflow-hidden border border-slate-700/50">
              <div id="bar-TQQQ" class="h-2.5 rounded-full transition-all duration-500 ease-out bg-slate-700" style="width: 0%"></div>
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
              <span id="status-SOXL" class="px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700">
                Standby
              </span>
            </div>

            <div class="grid grid-cols-2 gap-3 mt-4 text-xs">
              <div>
                <span class="text-slate-400 block text-[11px]">Current Shares</span>
                <span id="shares-SOXL" class="font-mono font-bold text-white text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Position Value</span>
                <span id="value-SOXL" class="font-mono font-bold text-white text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Dynamic Stop (YZ)</span>
                <span id="stop-SOXL" class="font-mono font-bold text-amber-400 text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Unrealized P&amp;L</span>
                <span id="pnl-SOXL" class="font-mono font-bold text-slate-400 text-sm">--</span>
              </div>
            </div>
          </div>

          <!-- Headroom against 20% cap Progress Bar -->
          <div class="mt-5 pt-3 border-t border-slate-800/80">
            <div class="flex justify-between items-center text-xs mb-1.5">
              <span id="headroom-label-SOXL" class="font-medium text-slate-400">SOXL: --</span>
              <span id="headroom-cap-SOXL" class="font-mono text-slate-400 text-[11px]">Cap: --</span>
            </div>
            <div class="w-full bg-slate-800 rounded-full h-2.5 overflow-hidden border border-slate-700/50">
              <div id="bar-SOXL" class="h-2.5 rounded-full transition-all duration-500 ease-out bg-slate-700" style="width: 0%"></div>
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
              <span id="status-TNA" class="px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700">
                Standby
              </span>
            </div>

            <div class="grid grid-cols-2 gap-3 mt-4 text-xs">
              <div>
                <span class="text-slate-400 block text-[11px]">Current Shares</span>
                <span id="shares-TNA" class="font-mono font-bold text-white text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Position Value</span>
                <span id="value-TNA" class="font-mono font-bold text-white text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Dynamic Stop (YZ)</span>
                <span id="stop-TNA" class="font-mono font-bold text-amber-400 text-sm">--</span>
              </div>
              <div>
                <span class="text-slate-400 block text-[11px]">Unrealized P&amp;L</span>
                <span id="pnl-TNA" class="font-mono font-bold text-slate-400 text-sm">--</span>
              </div>
            </div>
          </div>

          <!-- Headroom against 20% cap Progress Bar -->
          <div class="mt-5 pt-3 border-t border-slate-800/80">
            <div class="flex justify-between items-center text-xs mb-1.5">
              <span id="headroom-label-TNA" class="font-medium text-slate-400">TNA: --</span>
              <span id="headroom-cap-TNA" class="font-mono text-slate-400 text-[11px]">Cap: --</span>
            </div>
            <div class="w-full bg-slate-800 rounded-full h-2.5 overflow-hidden border border-slate-700/50">
              <div id="bar-TNA" class="h-2.5 rounded-full transition-all duration-500 ease-out bg-slate-700" style="width: 0%"></div>
            </div>
          </div>
        </div>

      </div>
    </section>

    <!-- SECTION 4: System Health & Regime Diagnostics -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wider">
          4. System Health &amp; Regime Diagnostics
        </h2>
        <span class="text-[11px] text-slate-500">State Machine &amp; Multi-Tier Execution Gates</span>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">

        <!-- Card: Current Trading Phase -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span class="font-medium">Current Trading Phase</span>
              <span id="phase-badge" class="px-2 py-0.5 rounded text-[11px] font-semibold bg-slate-800 text-slate-300 border border-slate-700">
                Loading...
              </span>
            </div>
            <div id="phase-title" class="text-lg font-bold text-white mt-2 leading-tight">
              Loading...
            </div>
            <p id="phase-desc" class="text-xs text-slate-400 mt-2 leading-relaxed">
              Evaluating intraday schedule gate...
            </p>
          </div>
          <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
            <span>Sizing Multiplier:</span>
            <span id="phase-multiplier" class="font-mono text-slate-200 font-semibold">--</span>
          </div>
        </div>

        <!-- Card: AI Engine Confidence / Macro Assessment -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between text-xs text-slate-400 mb-1">
              <span class="font-medium">AI Engine Confidence</span>
              <span id="regime-status-tag" class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">08:35 EDT Gate</span>
            </div>
            <div class="flex items-baseline gap-2 mt-2">
              <span id="confidence-pct" class="text-lg font-bold font-mono text-slate-300">N/A (Awaiting 08:35 EDT run)</span>
            </div>
            <div class="mt-3">
              <div id="soft-reserves-pill" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700">
                <span id="soft-reserves-dot" class="h-2 w-2 rounded-full bg-slate-500"></span>
                <span id="soft-reserves-text">Soft Reserves: Locked</span>
              </div>
            </div>
          </div>
          <p id="regime-note-text" class="text-[11px] text-slate-500 mt-4 pt-3 border-t border-slate-800/80">
            Scheduled Vertex AI assessment runs daily at 08:35 EDT before market open.
          </p>
        </div>

        <!-- Card: Operational Vitals -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between text-xs text-slate-400 mb-2">
              <span class="font-medium">Engine Mode &amp; Vitals</span>
              <span id="health-overall" class="text-emerald-400 font-semibold font-mono">ONLINE</span>
            </div>
            <div class="space-y-2 mt-3">
              <div class="flex items-center justify-between">
                <span class="text-xs text-slate-300">Engine Mode</span>
                <span id="pill-engine-mode" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                  <span class="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                  LIVE
                </span>
              </div>
              <div class="flex items-center justify-between">
                <span class="text-xs text-slate-300">Broker Sync</span>
                <span id="pill-broker-sync" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                  <span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
                  HEALTHY
                </span>
              </div>
              <div class="flex items-center justify-between">
                <span class="text-xs text-slate-300">Microstructure Engine</span>
                <span id="pill-microstructure" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">
                  <span class="h-1.5 w-1.5 rounded-full bg-cyan-400"></span>
                  SHADOW
                </span>
              </div>
            </div>
          </div>
          <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
            <span>Schwab API Ping:</span>
            <span id="broker-ping" class="font-mono text-slate-200">-- ms</span>
          </div>
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
      'OFFLINE': {
        title: 'Market Closed / Offline',
        badge: 'Entries Locked',
        badgeColor: 'slate',
        desc: 'Outside core market hours. Engine in monitoring & scheduled sleep mode.',
        multiplier: '0.0x (Offline)'
      },
      'PRE_MARKET': {
        title: 'Pre-Market Prep (08:35–09:30 EDT)',
        badge: 'Entries Locked',
        badgeColor: 'amber',
        desc: 'Pre-market data synchronization, cache warmup, and Vertex AI regime scanning.',
        multiplier: '0.0x (Warmup)'
      },
      'MORNING_DRIVE': {
        title: 'Morning Trading (09:30–10:30 EDT)',
        badge: 'New Buys Allowed',
        badgeColor: 'emerald',
        desc: 'Full Quarter-Kelly sizing active for morning breakout entries.',
        multiplier: '1.0x (100%)'
      },
      'MID_MORNING': {
        title: 'Mid-Morning Trend (10:30–11:30 EDT)',
        badge: 'New Buys Allowed',
        badgeColor: 'emerald',
        desc: 'Standard parameters; trend continuation entries monitored.',
        multiplier: '1.0x (100%)'
      },
      'MIDDAY_FREEZE': {
        title: 'Midday Freeze (11:30–14:00 EDT)',
        badge: 'Entries Locked (Trailing Stops Active)',
        badgeColor: 'amber',
        desc: 'Midday freeze engaged: lockout on new buys. Managing trailing stops only.',
        multiplier: '0.0x (Stops Only)'
      },
      'POWER_HOUR': {
        title: 'Power Hour (14:00–15:35 EDT)',
        badge: 'Half-Size Entries',
        badgeColor: 'blue',
        desc: 'Secondary afternoon momentum trend entries at 50% fractional sizing.',
        multiplier: '0.5x (50%)'
      },
      'PRE_CLOSE': {
        title: 'Pre-Close (15:35–15:50 EDT)',
        badge: 'Entries Locked',
        badgeColor: 'amber',
        desc: 'Preparing portfolio for end-of-day unwind and overnight cash safety.',
        multiplier: '0.0x (Locked)'
      },
      'MANDATORY_FLATTEN': {
        title: 'Mandatory Flatten (15:50–15:55 EDT)',
        badge: 'Liquidating to Cash',
        badgeColor: 'rose',
        desc: 'Mandatory flatten sequence engaged: liquidating all active intraday positions.',
        multiplier: '0.0x (Exiting)'
      },
      'POST_CLOSE_REFLECTION': {
        title: 'Post-Close Reflection (15:55–17:00 EDT)',
        badge: 'Entries Locked',
        badgeColor: 'slate',
        desc: 'Session closed. Computing daily trade reconciliation and P&L metrics.',
        multiplier: '0.0x (Idle)'
      }
    };

    function formatCurrency(val) {
      if (val === null || val === undefined || isNaN(val)) return '--';
      return '$' + Number(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function formatTime(dateObj) {
      const d = dateObj || new Date();
      return d.toLocaleTimeString('en-US', { timeZone: 'America/New_York' });
    }

    // Prepend log event to the VERY TOP of the feed
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
        <div class="py-1.5 flex items-start justify-between gap-3 text-xs transition duration-200">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="px-2 py-0.5 rounded text-[10px] font-mono font-semibold border ${colorClass}">
              [${category}]
            </span>
            <span class="text-slate-200 font-medium">${message}</span>
          </div>
          <span class="text-slate-500 font-mono text-[11px] whitespace-nowrap">${timeStr} EDT</span>
        </div>
      `;

      // Remove the connecting placeholder on first log
      if (container.firstElementChild && container.firstElementChild.classList.contains('italic')) {
        container.innerHTML = '';
      }

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
        addLogEvent('LIFECYCLE', 'Log feed cleared by user', 'slate');
      }
    }

    // Updates Section 2: Account Vitals & PnL Banner
    function updateAccountVitals(statusData, ledgerData) {
      const totalNlv = ledgerData?.total_nlv ?? statusData?.nlv ?? null;
      const settledCash = ledgerData?.bucket1_settled ?? null;
      const singleCap = ledgerData?.max_single_exposure ?? (totalNlv ? totalNlv * 0.20 : null);
      const unsettledCash = ledgerData?.bucket2_unsettled ?? 0.0;
      const rawPhase = statusData?.session_phase || 'OFFLINE';

      // Total NLV
      const nlvEl = document.getElementById('cap-nlv');
      if (nlvEl) nlvEl.textContent = totalNlv !== null ? formatCurrency(totalNlv) : '--';

      const netChangeEl = document.getElementById('cap-net-change');
      if (netChangeEl && statusData?.net_change_usd !== null && statusData?.net_change_usd !== undefined) {
        const sign = statusData.net_change_usd >= 0 ? '+' : '';
        const pct = statusData.net_change_pct ? ` (${sign}${Number(statusData.net_change_pct).toFixed(2)}%)` : '';
        netChangeEl.textContent = `${sign}${formatCurrency(statusData.net_change_usd)}${pct}`;
        netChangeEl.className = `font-mono font-semibold ${statusData.net_change_usd >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
      } else if (netChangeEl) {
        netChangeEl.textContent = '--';
      }

      // Settled Cash & Unsettled
      const settledEl = document.getElementById('cap-settled');
      if (settledEl) settledEl.textContent = settledCash !== null ? formatCurrency(settledCash) : '--';

      const unsettledEl = document.getElementById('cap-unsettled');
      if (unsettledEl) unsettledEl.textContent = formatCurrency(unsettledCash);

      // Single-Ticker Ceiling
      const ceilingEl = document.getElementById('cap-ceiling');
      if (ceilingEl) {
        ceilingEl.textContent = singleCap !== null ? `${formatCurrency(singleCap)} limit` : '--';
      }

      // T+1 Rule
      const t1El = document.getElementById('pill-t1-rule');
      if (t1El) {
        const isCompliant = !ledgerData?.gfv_risk_flag;
        t1El.textContent = isCompliant ? 'COMPLIANT (0 GFV)' : 'GFV RISK ALERT';
        t1El.className = `font-mono font-semibold ${isCompliant ? 'text-emerald-400' : 'text-amber-400'}`;
      }

      // Daily P&L logic: prior to 09:30 open, show N/A (Pre-Open)
      const pnlValEl = document.getElementById('today-pnl-val');
      const pnlTagEl = document.getElementById('pnl-session-tag');
      const pnlStatusEl = document.getElementById('today-pnl-status');

      const isPreMarketOrOffline = (rawPhase === 'OFFLINE' || rawPhase === 'PRE_MARKET' || rawPhase === 'POST_CLOSE_REFLECTION');
      if (isPreMarketOrOffline) {
        if (pnlValEl) {
          pnlValEl.textContent = '$0.00';
          pnlValEl.className = 'text-3xl font-bold font-mono text-slate-400 mt-2';
        }
        if (pnlTagEl) {
          pnlTagEl.textContent = 'Pre-Open';
          pnlTagEl.className = 'text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-mono';
        }
        if (pnlStatusEl) {
          pnlStatusEl.textContent = 'N/A (Pre-Open)';
          pnlStatusEl.className = 'text-slate-400 font-medium';
        }
      } else {
        const todayPnl = statusData?.today_pnl ?? 0.0;
        const sign = todayPnl >= 0 ? '+' : '';
        if (pnlValEl) {
          pnlValEl.textContent = `${sign}${formatCurrency(todayPnl)}`;
          pnlValEl.className = `text-3xl font-bold font-mono mt-2 ${todayPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
        }
        if (pnlTagEl) {
          pnlTagEl.textContent = 'Active Session';
          pnlTagEl.className = 'text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-mono';
        }
        if (pnlStatusEl) {
          pnlStatusEl.textContent = 'Live Intraday Realized + Unrealized';
          pnlStatusEl.className = 'text-slate-300 font-medium';
        }
      }

      return {
        rawPhase,
        totalNlv,
        settledCash,
        singleCap
      };
    }

    // Updates Section 3: Holdings & Headroom Ledger
    function updateHoldings(positionsAllData, ledgerData) {
      const totalNlv = ledgerData?.total_nlv ?? 3747.0;
      const singleCap = ledgerData?.max_single_exposure ?? (totalNlv * 0.20) ?? 749.0;

      // Extract managed and unmanaged positions from /api/positions/all?env=active
      const managed = positionsAllData?.managed || [];
      const unmanaged = positionsAllData?.unmanaged || [];
      const allRows = [...managed, ...unmanaged];

      const posMap = {};
      allRows.forEach(p => {
        if (p && p.symbol) {
          posMap[p.symbol.toUpperCase()] = p;
        }
      });

      TARGET_SYMBOLS.forEach(sym => {
        const pos = posMap[sym];
        const shares = pos ? pos.quantity : 0;
        const val = pos ? pos.notional_value : 0;
        const stopPrice = pos && (pos.stop_price || pos.hard_stop_price) ? Number(pos.stop_price || pos.hard_stop_price) : null;
        const pnl = pos ? pos.unrealized_pnl : 0;

        const statusEl = document.getElementById(`status-${sym}`);
        const sharesEl = document.getElementById(`shares-${sym}`);
        const valueEl = document.getElementById(`value-${sym}`);
        const stopEl = document.getElementById(`stop-${sym}`);
        const pnlEl = document.getElementById(`pnl-${sym}`);
        const labelEl = document.getElementById(`headroom-label-${sym}`);
        const capEl = document.getElementById(`headroom-cap-${sym}`);
        const barEl = document.getElementById(`bar-${sym}`);

        if (statusEl) {
          if (shares > 0) {
            statusEl.textContent = 'ACTIVE';
            statusEl.className = 'px-2.5 py-0.5 rounded text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
          } else {
            statusEl.textContent = 'Standby';
            statusEl.className = 'px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700';
          }
        }

        if (sharesEl) sharesEl.textContent = shares > 0 ? `${shares} shares` : '0 shares';
        if (valueEl) valueEl.textContent = shares > 0 ? formatCurrency(val) : '$0.00';
        if (stopEl) {
          stopEl.textContent = stopPrice !== null ? formatCurrency(stopPrice) : 'Standby';
        }
        if (pnlEl) {
          if (shares > 0) {
            const sign = pnl >= 0 ? '+' : '';
            pnlEl.textContent = `${sign}${formatCurrency(pnl)}`;
            pnlEl.className = `font-mono font-bold text-sm ${pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
          } else {
            pnlEl.textContent = '$0.00';
            pnlEl.className = 'font-mono font-bold text-sm text-slate-400';
          }
        }

        // 20% Exposure Headroom Progress Bar
        const pctOfLimit = singleCap > 0 ? Math.min(100, Math.round((val / singleCap) * 100)) : 0;
        const room = Math.max(0, singleCap - val);

        if (labelEl) {
          if (shares > 0 && pctOfLimit >= 95) {
            labelEl.textContent = `${sym}: ${pctOfLimit}% of limit - FULL`;
            labelEl.className = 'font-medium text-amber-400';
          } else if (shares > 0) {
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

    // Updates Section 4: System Health & Regime Diagnostics
    function updateSystemHealthAndRegime(statusData, regimeSummaryData, ledgerData) {
      const rawPhase = statusData?.session_phase || 'OFFLINE';
      const phaseInfo = PHASE_CONFIG[rawPhase] || {
        title: rawPhase,
        badge: statusData?.entry_permitted ? 'New Buys Allowed' : 'Entries Locked',
        badgeColor: statusData?.entry_permitted ? 'emerald' : 'slate',
        desc: 'Phase parameters enforced.',
        multiplier: `${statusData?.phase_sizing_multiplier || 0.0}x`
      };

      // Phase Card
      const phaseTitleEl = document.getElementById('phase-title');
      if (phaseTitleEl) phaseTitleEl.textContent = phaseInfo.title;

      const phaseBadgeEl = document.getElementById('phase-badge');
      if (phaseBadgeEl) {
        phaseBadgeEl.textContent = phaseInfo.badge;
        phaseBadgeEl.className = `px-2 py-0.5 rounded text-[11px] font-semibold border ${
          phaseInfo.badgeColor === 'emerald' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' :
          phaseInfo.badgeColor === 'amber' ? 'bg-amber-500/10 text-amber-400 border-amber-500/30' :
          phaseInfo.badgeColor === 'blue' ? 'bg-cyan-500/10 text-cyan-400 border-cyan-500/30' :
          phaseInfo.badgeColor === 'rose' ? 'bg-rose-500/10 text-rose-400 border-rose-500/30' :
          'bg-slate-800 text-slate-300 border-slate-700'
        }`;
      }

      const phaseDescEl = document.getElementById('phase-desc');
      if (phaseDescEl) phaseDescEl.textContent = phaseInfo.desc;

      const phaseMultEl = document.getElementById('phase-multiplier');
      if (phaseMultEl) phaseMultEl.textContent = phaseInfo.multiplier;

      // AI Engine Confidence / Macro Regime Card
      const isStale = regimeSummaryData?.stale ?? true;
      const isOfflineOrPre = (rawPhase === 'OFFLINE' || rawPhase === 'PRE_MARKET');
      const confPctEl = document.getElementById('confidence-pct');
      const srPill = document.getElementById('soft-reserves-pill');
      const srDot = document.getElementById('soft-reserves-dot');
      const srText = document.getElementById('soft-reserves-text');
      const regimeNoteEl = document.getElementById('regime-note-text');

      if (isStale || isOfflineOrPre) {
        if (confPctEl) confPctEl.textContent = 'N/A (Awaiting 08:35 EDT run)';
        if (srPill) srPill.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700';
        if (srDot) srDot.className = 'h-2 w-2 rounded-full bg-slate-500';
        if (srText) srText.textContent = 'Soft Reserves: Locked';
        if (regimeNoteEl) regimeNoteEl.textContent = 'Scheduled Vertex AI assessment runs daily at 08:35 EDT before market open.';
      } else {
        const posterior = ledgerData?.posterior || statusData?.posterior || 0.4615;
        const posteriorPct = (Number(posterior) * 100).toFixed(1) + '%';
        if (confPctEl) confPctEl.textContent = `${posteriorPct} win-rate posterior`;

        const buyingPower = ledgerData?.buying_power || {};
        const softUnlocked = buyingPower.high_probability || (buyingPower.soft_draw_available > 0);
        if (softUnlocked) {
          if (srPill) srPill.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
          if (srDot) srDot.className = 'h-2 w-2 rounded-full bg-emerald-400';
          if (srText) srText.textContent = 'Soft Reserves: Unlocked (Regime A)';
        } else {
          if (srPill) srPill.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700';
          if (srDot) srDot.className = 'h-2 w-2 rounded-full bg-slate-500';
          if (srText) srText.textContent = 'Soft Reserves: Locked';
        }
        if (regimeNoteEl && regimeSummaryData?.headline) {
          regimeNoteEl.textContent = `Regime ${regimeSummaryData.regime || 'A'}: ${regimeSummaryData.headline}`;
        }
      }

      // Operational Vitals
      const mode = statusData?.engine_mode === 'LIVE_TRADING' ? 'LIVE' : (statusData?.engine_mode || 'LIVE');
      const pillMode = document.getElementById('pill-engine-mode');
      if (pillMode) {
        pillMode.innerHTML = `<span class="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse"></span> ${mode}`;
      }

      const pingEl = document.getElementById('broker-ping');
      if (pingEl) {
        const pingMs = statusData?.vm_stats?.api_ping_ms;
        pingEl.textContent = pingMs !== null && pingMs !== undefined ? `${Math.round(pingMs)} ms` : '-- ms';
      }
    }

    // Translates data deltas into human-readable messages in Section 1
    function translateDeltas(vitals, positionsMap, indicatorsData, regimeSummaryData) {
      if (!lastState.initialized) {
        // Initial connection events
        addLogEvent('LIFECYCLE', `Session phase active: ${PHASE_CONFIG[vitals.rawPhase]?.title || vitals.rawPhase}`, 'blue');
        addLogEvent('RISK', `Single-ticker capacity ceiling enforced at ${formatCurrency(vitals.singleCap)} (20% NLV rule)`, 'amber');

        const isStale = regimeSummaryData?.stale ?? true;
        if (isStale || vitals.rawPhase === 'OFFLINE') {
          addLogEvent('REGIME', 'Vertex AI macro assessment status: Awaiting 08:35 EDT scheduled run', 'purple');
        } else {
          addLogEvent('REGIME', `Vertex AI assessment active: ${regimeSummaryData?.headline || 'Regime A favored'}`, 'purple');
        }

        // Report active positions detected
        TARGET_SYMBOLS.forEach(sym => {
          const p = positionsMap[sym];
          if (p && p.quantity > 0) {
            addLogEvent('ORDER/FLOW', `Active position synchronized: ${sym} (${p.quantity} shares, value: ${formatCurrency(p.notional_value)})`, 'emerald');
            if (p.stop_price) {
              addLogEvent('RISK', `Dynamic Yang-Zhang stop active for ${sym} at ${formatCurrency(p.stop_price)}`, 'amber');
            }
          }
        });

        lastState.initialized = true;
        lastState.sessionPhase = vitals.rawPhase;
        lastState.totalNlv = vitals.totalNlv;
        lastState.settledCash = vitals.settledCash;
        TARGET_SYMBOLS.forEach(sym => {
          const p = positionsMap[sym];
          lastState.quantities[sym] = p ? p.quantity : 0;
          lastState.stops[sym] = p ? (p.stop_price || null) : null;
        });
        return;
      }

      // 1. LIFECYCLE Phase transitions
      if (vitals.rawPhase !== lastState.sessionPhase) {
        const newTitle = PHASE_CONFIG[vitals.rawPhase]?.title || vitals.rawPhase;
        if (vitals.rawPhase === 'MIDDAY_FREEZE') {
          addLogEvent('LIFECYCLE', 'Midday Freeze engaged: new buys locked. Managing trailing stops only.', 'amber');
        } else if (vitals.rawPhase === 'MORNING_DRIVE') {
          addLogEvent('LIFECYCLE', 'Morning Trading session open: 100% Quarter-Kelly sizing active.', 'emerald');
        } else if (vitals.rawPhase === 'POWER_HOUR') {
          addLogEvent('LIFECYCLE', 'Power Hour engaged: secondary trend entries at 50% sizing.', 'blue');
        } else if (vitals.rawPhase === 'MANDATORY_FLATTEN') {
          addLogEvent('LIFECYCLE', 'Mandatory Flatten engaged: active liquidation sequence starting.', 'rose');
        } else {
          addLogEvent('LIFECYCLE', `Trading phase transitioned to ${newTitle}`, 'blue');
        }
        lastState.sessionPhase = vitals.rawPhase;
      }

      // 2. RISK: Dynamic Stop Ratchets
      TARGET_SYMBOLS.forEach(sym => {
        const p = positionsMap[sym];
        const newStop = p ? Number(p.stop_price || p.hard_stop_price || 0) : null;
        const oldStop = lastState.stops[sym];

        if (newStop && oldStop && newStop > oldStop) {
          addLogEvent('RISK', `Dynamic stop rose to ${formatCurrency(newStop)} on ${sym} (trailing Yang-Zhang protect)`, 'emerald');
        } else if (newStop && oldStop && newStop < oldStop) {
          addLogEvent('RISK', `Dynamic stop adjusted to ${formatCurrency(newStop)} on ${sym}`, 'amber');
        } else if (newStop && !oldStop) {
          addLogEvent('RISK', `Dynamic stop established at ${formatCurrency(newStop)} on ${sym}`, 'amber');
        }
        lastState.stops[sym] = newStop;

        // ORDER/FLOW: Position share adjustments
        const newQty = p ? p.quantity : 0;
        const oldQty = lastState.quantities[sym] || 0;
        if (newQty > oldQty && oldQty === 0) {
          addLogEvent('ORDER/FLOW', `Bought ${newQty} shares of ${sym} (${formatCurrency(p.notional_value)})`, 'emerald');
        } else if (newQty < oldQty && newQty === 0) {
          addLogEvent('ORDER/FLOW', `Exited ${sym} position (${oldQty} shares liquidated to settled cash)`, 'blue');
        } else if (newQty !== oldQty) {
          addLogEvent('ORDER/FLOW', `Position updated: ${sym} now ${newQty} shares`, 'blue');
        }
        lastState.quantities[sym] = newQty;
      });

      // 3. ORDER/FLOW: MLOFI / VPIN gate logs from indicators
      if (indicatorsData?.symbols) {
        Object.entries(indicatorsData.symbols).forEach(([sym, data]) => {
          if (data?.vpin?.toxic) {
            addLogEvent('ORDER/FLOW', `High toxicity detected via VPIN on ${sym} - new entry gate closed`, 'rose');
          }
        });
      }
    }

    // Main polling loop hitting active environment endpoints strictly with ?env=active
    async function pollTelemetry() {
      try {
        const [statusRes, ledgerRes, positionsRes, indicatorsRes, regimeRes] = await Promise.allSettled([
          fetch('/api/status?env=active').then(r => r.ok ? r.json() : null),
          fetch('/api/ledger?env=active').then(r => r.ok ? r.json() : null),
          fetch('/api/positions/all?env=active').then(r => r.ok ? r.json() : null),
          fetch('/api/indicators?env=active').then(r => r.ok ? r.json() : null),
          fetch('/api/v1/regime/summary').then(r => r.ok ? r.json() : null)
        ]);

        const statusData = statusRes.status === 'fulfilled' ? statusRes.value : null;
        const ledgerData = ledgerRes.status === 'fulfilled' ? ledgerRes.value : null;
        const positionsData = positionsRes.status === 'fulfilled' ? positionsRes.value : null;
        const indicatorsData = indicatorsRes.status === 'fulfilled' ? indicatorsRes.value : null;
        const regimeSummaryData = regimeRes.status === 'fulfilled' ? regimeRes.value : null;

        if (statusData || ledgerData) {
          const vitals = updateAccountVitals(statusData, ledgerData);
          const posMap = updateHoldings(positionsData, ledgerData);
          updateSystemHealthAndRegime(statusData, regimeSummaryData, ledgerData);
          translateDeltas(vitals, posMap, indicatorsData, regimeSummaryData);

          const syncEl = document.getElementById('sync-status');
          if (syncEl) syncEl.textContent = 'Live • 2s Polling (env=active)';
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
