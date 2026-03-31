/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './backend/templates/**/*.html',
    './backend/events/templatetags/*.py',
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
