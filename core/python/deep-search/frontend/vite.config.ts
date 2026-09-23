import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react-swc";
import { defineConfig } from "vite";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/app/",
  resolve: {
    alias: {
      "@": path.resolve(new URL(".", import.meta.url).pathname, "./src"),
    },
  },
  server: {
    // Makes the server accessible on the local network (e.g., for mobile testing)
    host: true,
    // Should be disabled or limited when deployed in untrusted network environments.
    allowedHosts: true,
    proxy: {
      // Proxy API requests to the backend server
      "/api": {
        target: "http://127.0.0.1:8000", // Default backend address
        changeOrigin: true,
        secure: false,
        rewrite: (path) => path.replace(/^\/api/, ""),
        configure: (proxy) => {
          proxy.on("error", (err) => {
            console.error("proxy error", err);
          });
          proxy.on("proxyReq", (_proxyReq, req) => {
            console.debug(
              "Sending Request to the Target:",
              req.method,
              req.url,
            );
          });
          proxy.on("proxyRes", (proxyRes, req) => {
            console.debug(
              "Received Response from the Target:",
              proxyRes.statusCode,
              req.url,
            );
          });
        },
      },
    },
  },
});
