/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Retain backward-compatible aliases for unrefactored components
        background: '#080c14',
        'surface-highlight': '#202a3e',
        border: '#263145',

        // M3 Dark Theme Surface Containers (Tone 4 to Tone 22)
        surface: {
          'container-lowest': '#080c14', // Tone 4 - deep canvas viewport & recessed areas
          'dim': '#0d121c',              // Tone 6
          DEFAULT: '#101522',            // Tone 10 - screen baseline
          'bright': '#343a4a',           // Tone 24
          'container-low': '#131926',    // Tone 10 - sidebar navigation rail & backplanes
          'container': '#182030',        // Tone 12 - cards, toolbars, primary panels
          'container-high': '#202a3e',   // Tone 17 - active toggles, hover states, chips
          'container-highest': '#28344c',// Tone 22 - floating dialogs, menus, modals
        },

        // M3 Content & Text Roles
        'on-surface': '#e2e7f2',         // Tone 90 - high emphasis primary typography
        'on-surface-variant': '#98a2b5', // Tone 70 - medium emphasis labels & structure
        'outline': '#5c667a',            // Tone 50 - interactive focus boundaries
        'outline-variant': '#263145',    // Tone 25 - ultra-subtle hairline dividers (15% opacity)

        // M3 Primary & Functional Roles
        primary: {
          DEFAULT: '#67d6ed',            // Electric Cyan Tone 80 (Focus & active traces)
          container: '#004f5b',          // Cyan Container Tone 30
          'on-primary': '#00363f',
          'on-container': '#97f0ff',
        },
        secondary: {
          DEFAULT: '#a6c8e2',            // Slate / Steel Tone 80
          container: '#2a485e',
          'on-secondary': '#0c3346',
          'on-container': '#cde5ff',
        },
        tertiary: {
          DEFAULT: '#e5c158',            // Gold / Neutral Accent Tone 80
          container: '#564400',
          'on-tertiary': '#3c2f00',
          'on-container': '#ffe08b',
        },
        error: {
          DEFAULT: '#ff897d',            // High-visibility Coral Tone 80
          container: '#8e1410',
          'on-error': '#410002',
          'on-error-container': '#ffdad6',
        },

        // Financial Semantic Tonal Spots
        finance: {
          bull: '#6dd58c',               // Green Tone 80 - Profit / Upper Bound
          'bull-container': '#0a3a1b',
          'on-bull': '#003915',
          'on-bull-container': '#89f2a6',

          bear: '#ff897d',               // Red Tone 80 - Loss / Lower Bound
          'bear-container': '#5c0004',
          'on-bear': '#410002',
          'on-bear-container': '#ffdad6',

          warning: '#fbbf24',            // Amber Tone 80 - CHOP / Volatility Squeeze
          'warning-container': '#451a03',
          'on-warning': '#270e00',
          'on-warning-container': '#ffe086',

          supremum: '#7dd3fc',           // Sky Tone 80 - Supremum M(t) envelope crest
          'supremum-container': '#07486a',

          stale: '#f43f5e',              // Rose Tone 80 - Socket Stale Warning (>2s)
          'cone-fill': 'rgba(103, 214, 237, 0.08)', // Volatility cone band fill
        },
      },
      boxShadow: {
        // M3 Ambient + Key Elevation System
        'm3-1': '0 1px 2px 0 rgba(0, 0, 0, 0.35), 0 1px 3px 1px rgba(0, 0, 0, 0.15)',
        'm3-2': '0 1px 2px 0 rgba(0, 0, 0, 0.35), 0 2px 6px 2px rgba(0, 0, 0, 0.20)',
        'm3-3': '0 4px 8px 3px rgba(0, 0, 0, 0.30), 0 1px 3px 0 rgba(0, 0, 0, 0.35)',
        'm3-4': '0 6px 10px 4px rgba(0, 0, 0, 0.35), 0 2px 3px 0 rgba(0, 0, 0, 0.40)',
      },
      borderRadius: {
        // M3 Standard Shape Scale
        'xs': '4px',
        'sm': '8px',
        'md': '12px',
        'lg': '16px',
        'xl': '24px',
        '2xl': '28px',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}
