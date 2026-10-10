import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  // The checked-in default preserves the existing local workflow.  P10 browser
  // acceptance can point only this Vite dev proxy at the isolated API without
  // changing production configuration or the public port.
  const apiProxy = loadEnv(mode, '.', '').P10_API_PROXY ?? 'http://127.0.0.1:18000';
  return {
    plugins: [react()],
    server: {
      port: 5173, strictPort: true,
      proxy: {
        '/api': { target: apiProxy, ws: true },
        '/health': { target: apiProxy },
      },
    },
  };
});
