// 函数层 · numeric 类别：前端金额换算函数的 property_based_test。
//
// 直接 import 前端线上同款模块（src/api/banquetApi.js），不复刻实现。
// 随机数用固定种子的 LCG：每次跑同一批输入，结果可比对、可复现。
//
// 属性（应该成立的关系）：
//   FP1 往返闭合：分 -> 元 -> 分 必须回到原值
//   FP2 分转元精确：centToYuan(x) * 100 必须仍然等于 x
//   FP3 元转分正确：yuanToCent(y) 必须等于 round(y * 100)
//   FP4 大数精度：超过 2^53 分之后，JS Number 会丢精度 —— 量化丢了多少
//   FP5 非法输入语义：非数字 / 布尔 / 超长值分别被怎么处理
//
// stdout: JSON

import { yuanToCent, centToYuan } from "/Users/june/jiuyan/src/api/banquetApi.js";

function lcg(seed) {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

const rnd = lcg(20261107);
const MAX_SAFE = Number.MAX_SAFE_INTEGER; // 2^53 - 1

function randomInt(min, max) {
  return min + Math.floor(rnd() * (max - min + 1));
}

const results = {};

// 换算函数在源码里的位置（供 finding 的 location 用）
{
  const fs = await import("node:fs");
  const apiPath = "/Users/june/jiuyan/src/api/banquetApi.js";
  const src = fs.readFileSync(apiPath, "utf8");
  const locate = (name) => {
    const idx = src.indexOf(`export function ${name}(`);
    if (idx < 0) return null;
    return { file: apiPath, line: src.slice(0, idx).split("\n").length, symbol: name };
  };
  results.sources = {
    yuanToCent: locate("yuanToCent"),
    centToYuan: locate("centToYuan"),
  };
}

// FP1 往返闭合：分 -> 元 -> 分
{
  const failures = [];
  for (let i = 0; i < 2000; i++) {
    const x = randomInt(0, 10 ** 12);
    const back = yuanToCent(centToYuan(x));
    if (back !== x) failures.push({ cent: x, back, yuan: centToYuan(x) });
  }
  results.FP1_roundtrip = {
    property: "yuanToCent(centToYuan(x)) === x，对 0..10^12 的随机分值",
    ok: failures.length === 0,
    checked: 2000,
    failures: failures.slice(0, 5),
    failure_count: failures.length,
  };
}

// FP2 显示精度：界面显示的是 centToYuan(x) 再取 2 位小数，
// 显示值换回分必须仍然等于库里存的分值（这是用户真正看到的东西）
{
  const failures = [];
  for (let i = 0; i < 2000; i++) {
    const x = randomInt(0, 10 ** 12);
    // 用精确十进制字符串比对：x 分 = floor(x/100) 元 + 两位小数
    const exact = `${Math.floor(x / 100)}.${String(x % 100).padStart(2, "0")}`;
    const shown = centToYuan(x).toFixed(2);
    if (shown !== exact) failures.push({ cent: x, shown, exact });
  }
  results.FP2_display_precision = {
    property: "centToYuan(x).toFixed(2) 必须等于 x 分的精确十进制展开，对 0..10^12 的随机分值",
    ok: failures.length === 0,
    checked: 2000,
    failures: failures.slice(0, 5),
    failure_count: failures.length,
  };
}

// FP3 元转分正确（2 位小数字符串，模拟表单来的值）
{
  const failures = [];
  for (let i = 0; i < 2000; i++) {
    const cents = randomInt(0, 10 ** 8);
    const y = (cents / 100).toFixed(2);
    const got = yuanToCent(y);
    const want = Math.round(Number(y) * 100);
    if (got !== want) failures.push({ yuan: y, got, want });
  }
  results.FP3_yuan_to_cent = {
    property: "yuanToCent(y) === Math.round(y * 100)，y 为 2 位小数字符串",
    ok: failures.length === 0,
    checked: 2000,
    failures: failures.slice(0, 5),
    failure_count: failures.length,
  };
}

// FP4 大数精度：分值超过 2^53 之后，JS Number 表示不了每一个整数。
// 用 BigInt 拿到「真实存在的那个分值」，再让 Number 去装它，看丢了多少。
{
  const bigCents = [
    9007199254740991n,   // 2^53 - 1，Number 仍能精确表示
    9007199254740993n,   // 2^53 + 1，Number 无法精确表示
    9223372036854775807n, // Long.MAX_VALUE，后端能存进 BIGINT
  ];
  const rows = bigCents.map((big) => {
    const asNumber = Number(big);
    const yuan = centToYuan(asNumber);
    const shown = Number(yuan.toFixed(2));
    const recovered = BigInt(Math.round(shown * 100));
    return {
      cent_exact: big.toString(),
      number_holds: BigInt(asNumber) === big,
      yuan_displayed: yuan,
      cent_after_display: recovered.toString(),
      lost_cent: (big - recovered).toString(),
    };
  });
  results.FP4_large_value_precision = {
    property: "分值超过 2^53 后，界面显示的金额再换回分必须仍等于原值",
    ok: rows.every((r) => r.lost_cent === "0"),
    samples: rows,
  };
}

// FP5 非法 / 边界输入的实际行为
{
  const cases = [
    { label: "boolean true", value: true },
    { label: "boolean false", value: false },
    { label: "空字符串", value: "" },
    { label: "非数字串", value: "abc" },
    { label: "数字串", value: "12.34" },
    { label: "负数", value: -1.5 },
    { label: "超 Long 上限", value: 1e21 },
    { label: "超长数字串", value: "9".repeat(40) },
    { label: "三位小数", value: 19.999 },
  ];
  results.FP5_invalid_input_semantics = {
    property: "记录非数字/布尔/超长输入被怎么处理（语义是否可预期）",
    ok: true,
    checked: cases.length,
    rows: cases.map((c) => ({
      label: c.label,
      input: c.value,
      yuan_to_cent: yuanToCent(c.value),
      cent_to_yuan: centToYuan(c.value),
    })),
  };
}

// ---------------------------------------------------------------------------
// 金额输入规范化：App.jsx 里的 normalizeMoneyInput 是所有金额输入框的唯一入口
// （成交价 / 定金 / 套餐价 / 商品价）。这里把 shipped 源码抽出来直接执行，
// 不是复刻实现 —— 抽不出来就报错，不静默跳过。
// ---------------------------------------------------------------------------
function extractFunction(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`在 App.jsx 里找不到 ${name}`);
  const line = source.slice(0, start).split("\n").length;
  let depth = 0;
  let i = source.indexOf("{", start);
  const bodyStart = i;
  for (; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) break;
    }
  }
  if (depth !== 0) throw new Error(`${name} 的大括号没配平`);
  return { src: source.slice(start, i + 1), line };
}

{
  const fs = await import("node:fs");
  const appPath = process.env.JIUYAN_FE_APP || "/Users/june/jiuyan/src/App.jsx";
  const src = fs.readFileSync(appPath, "utf8");
  const extracted = extractFunction(src, "normalizeMoneyInput");
  const normalizeMoneyInput = new Function(`return (${extracted.src})`)();

  // NP1 不得静默改变金额的方向
  const signCases = ["-19.99", "-0.01", "-100"];
  const signFailures = signCases
    .map((raw) => ({ input: raw, normalized: normalizeMoneyInput(raw) }))
    .filter((r) => !r.normalized.startsWith("-") && Number(r.normalized) > 0);

  // NP2 不得静默截断金额（3 位小数要么四舍五入、要么拒绝，不能悄悄少收）
  const truncCases = ["19.995", "0.005", "19.999"];
  const truncFailures = truncCases
    .map((raw) => {
      const normalized = normalizeMoneyInput(raw);
      const want = Math.round(Number(raw) * 100);
      const got = Math.round(Number(normalized) * 100);
      return { input: raw, normalized, want_cent: want, got_cent: got, lost_cent: want - got };
    })
    .filter((r) => r.lost_cent !== 0);

  // NP3 规范化结果必须仍是合法金额
  const shapeCases = ["", "abc", "1.2.3", "..", "1..2"];
  const shapeRows = shapeCases.map((raw) => {
    const normalized = normalizeMoneyInput(raw);
    const numeric = Number(normalized);
    return {
      input: raw,
      normalized,
      is_number: Number.isFinite(numeric),
      is_amount_shape: /^(0|[1-9]\d*)(\.\d{1,2})?$/.test(normalized),
    };
  });

  results.NP_money_input_normalization = {
    property: "金额输入规范化不得静默改变数值（方向 / 精度）",
    ok: signFailures.length === 0 && truncFailures.length === 0,
    source_file: appPath,
    source_line: extracted.line,
    symbol: "normalizeMoneyInput",
    sign_failures: signFailures,
    truncation_failures: truncFailures,
    shape_rows: shapeRows,
  };
}

process.stdout.write(JSON.stringify(results));
