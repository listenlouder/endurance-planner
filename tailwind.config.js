/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
      './backend/templates/**/*.html',
      './backend/templates/partials/**/*.html',
      './backend/events/**/*.py',
    ],
  theme: {
    extend: {},
  },
  plugins: [],
}
