import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 使用 fileURLToPath 将 file:// URL 转换为当前操作系统的文件路径。
// 不能直接读取 URL.pathname：它在 Windows 下会生成 /C:/...，且不会解码空格和中文。
const frontendRoot = fileURLToPath(new URL(".", import.meta.url));
const staticOutputDir = fileURLToPath(new URL("../app/static", import.meta.url));

export default defineConfig(({ command }) => ({
  root: frontendRoot,
  // 开发服务器从根路径提供页面；构建产物由 FastAPI 挂在 /static 下。
  base: command === "build" ? "/static/" : "/",
  plugins: [react()],
  build: {
    // 使用绝对路径，避免 Vite 在 Windows 下从错误的工作目录解析 ../app/static。
    outDir: staticOutputDir,
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
