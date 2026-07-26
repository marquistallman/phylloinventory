import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // Colores oficiales Colsubsidio
        bAmarillo: "#FFD000",   // Pantone 109 C
        bAzul: "#0067B1",       // Pantone 2196 C
        bGris: "#575756",       // Pantone Cool Gray 11 C
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};

export default config;
