/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    // i componenti fuori da app/ devono essere scansionati, altrimenti le classi
    // usate solo in Nav.tsx non vengono generate e il menu esce senza stile
    './components/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: { extend: {} },
  plugins: [],
}
