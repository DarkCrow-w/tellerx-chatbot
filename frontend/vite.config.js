import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  root: new URL(".", import.meta.url).pathname,
  // 开发服务器从根路径提供页面；构建产物由 FastAPI 挂在 /static 下。
  base: command === "build" ? "/static/" : "/",
  plugins: [react()],
  build: {
    outDir: "../app/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": `http://127.0.0.1:${process.env.TELLERX_API_PORT || "8000"}`,
      "/health": `http://127.0.0.1:${process.env.TELLERX_API_PORT || "8000"}`,
    },
  },
}));
