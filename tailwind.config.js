/** Tailwind config — static build to replace the slow Play CDN.
 *  Rebuild after editing templates:  npm run css   (see package.json)
 */
module.exports = {
  darkMode: "class",
  content: [
    "./**/templates/**/*.html",
    "./**/static/**/*.js",
    "!./node_modules/**",
    "!./staticfiles/**",
  ],
  // Classes that are built dynamically in JS (not literally in the HTML) and
  // must never be purged.
  safelist: [
    "active",
    "special-link",
    "pwa-installed",
    { pattern: /^(bg|text|border|from|to)-(primary|accent)-(50|100|200|300|400|500|600|700|800|900)$/ },
    { pattern: /^animate-(fade-in|slide-up|slide-down|pulse-slow|bounce-gentle)$/ },
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#f0f9ff', 100: '#e0f2fe', 200: '#bae6fd', 300: '#7dd3fc',
          400: '#38bdf8', 500: '#0ea5e9', 600: '#0284c7', 700: '#0369a1',
          800: '#075985', 900: '#0c4a6e',
        },
        accent: {
          50: '#fdf2f8', 100: '#fce7f3', 200: '#fbcfe8', 300: '#f9a8d4',
          400: '#f472b6', 500: '#ec4899', 600: '#db2777', 700: '#be185d',
          800: '#9d174d', 900: '#831843',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      animation: {
        'fade-in': 'fadeIn 0.5s ease-in-out',
        'slide-up': 'slideUp 0.3s ease-out',
        'slide-down': 'slideDown 0.3s ease-out',
        'pulse-slow': 'pulse 3s ease-in-out infinite',
        'bounce-gentle': 'bounceGentle 2s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
        slideUp: { '0%': { transform: 'translateY(10px)', opacity: '0' }, '100%': { transform: 'translateY(0)', opacity: '1' } },
        slideDown: { '0%': { transform: 'translateY(-10px)', opacity: '0' }, '100%': { transform: 'translateY(0)', opacity: '1' } },
        bounceGentle: { '0%, 100%': { transform: 'translateY(0)' }, '50%': { transform: 'translateY(-5px)' } },
      },
    },
  },
  plugins: [],
};
