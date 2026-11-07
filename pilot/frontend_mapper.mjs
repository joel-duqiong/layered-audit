// 用前端真实的 mapper（ship 在 src/api/banquetApi.js 里）处理 API 响应，
// 产出前端的视图模型。stdin: JSON；stdout: JSON。
import { mapProduct, mapPackage, mapOrder } from "/Users/june/jiuyan/src/api/banquetApi.js";

let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const input = JSON.parse(raw || "{}");

const out = {};
if (input.product) out.product = mapProduct(input.product);
if (input.package) out.package = mapPackage(input.package);
if (input.order) out.order = mapOrder(input.order);
if (input.products) out.products = input.products.map((it) => mapProduct(it));
if (input.packages) out.packages = input.packages.map((it) => mapPackage(it));

process.stdout.write(JSON.stringify(out));
