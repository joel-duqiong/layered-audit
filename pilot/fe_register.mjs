import { register } from "node:module";
globalThis.__VITE_ENV__ = { VITE_API_BASE_URL: process.env.JIUYAN_BASE_URL || "http://127.0.0.1:8081" };
globalThis.window = globalThis.window || { setTimeout, clearTimeout };
register("./fe_loader.mjs", import.meta.url);
