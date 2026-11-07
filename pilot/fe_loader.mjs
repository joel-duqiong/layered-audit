// 把前端源码里的 import.meta.env 换成可注入的全局对象，
// 让 Vite 的前端模块能在 Node 里直接 import（不改任何前端源码）。
export async function load(url, context, nextLoad) {
  const result = await nextLoad(url, context);
  if (url.startsWith("file://") && url.endsWith(".js")) {
    const source = result.source.toString().replaceAll("import.meta.env", "globalThis.__VITE_ENV__");
    return { ...result, source, shortCircuit: true };
  }
  return result;
}
