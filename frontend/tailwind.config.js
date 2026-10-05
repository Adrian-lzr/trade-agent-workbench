/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: "hsl(var(--primary))",
        muted: "hsl(var(--muted))",
        destructive: "hsl(var(--destructive))",
      },
      borderRadius: { sm: "calc(var(--radius) - 2px)", md: "var(--radius)" },
    },
  },
  plugins: [],
};
