# 等价性基线 —— 用来证明「通用化没有改变结论」

这两份是**通用化改造之前**的结论快照。留着它们，是为了让下面这句话
永远可复核，而不是只存在于某次会话里：

> 文件层 / 类层载体通用化之后，jiuyan 的判定结论与改造前**逐条一致**。

## 怎么复核

```bash
cd pilot
python3 - <<'EOF'
import json

def norm(path):
    items = json.load(open(path))
    out = {}
    for f in items:
        out[f.get("id") or f.get("scenario")] = {
            k: f.get(k) for k in
            ("severity", "layer", "detail", "title", "category", "claim")
        }
    return out

for base, cur in [("baselines/pre_generic_file_findings.json", "file_findings.json"),
                  ("baselines/pre_generic_class_findings.json", "class_findings.json")]:
    b, c = norm(base), norm(cur)
    diffs = [(k, fld) for k in set(b) & set(c) for fld in b[k]
              if b[k][fld] != c[k].get(fld)]
    print(f"{cur}: 基线 {len(b)} 条 / 现在 {len(c)} 条"
          f"｜只在基线 {sorted(set(b)-set(c))}｜只出现在 {sorted(set(c)-set(b))}"
          f"｜结论级差异 {len(diffs)} 条 {diffs[:5]}")
EOF
```

## 复核时要注意的两处「已知非回归差异」

这两条是**实测值**（每次跑都会变），不是结论变化：

| 文件 | finding | 差异 |
|---|---|---|
| pre_generic_class_findings.json | `INDEX_UNUSABLE_FOR_QUERY_PATTERN` | title 里的 EXPLAIN 实测耗时与倍数（ms / 倍数） |
| pre_generic_file_findings.json | `FILE-XFF-CLIENT-IP-SPOOF` | evidence 里的日志截取方式（取尾部 → 围绕标记截） |

结论字段（id / severity / layer / detail / category / claim）两边逐字节一致。

## 实际复核输出（2026-11-08 实测）

```text
file_findings.json: 基线 18 条 / 现在 18 条｜只在基线 []｜只出现在 []｜结论级差异 0 条 []
class_findings.json: 基线 15 条 / 现在 17 条｜只在基线 []｜
  只出现在 ['CLASS-ENCAPSULATION-LEAK-ENTITY-COLLECTION', 'CLASS-ENTITY-WITHOUT-VALUE-EQUALITY']｜
  结论级差异 1 条 [('INDEX_UNUSABLE_FOR_QUERY_PATTERN', 'title')]
```

## 注意两件事

1. **计数会被 id 去重影响**：类层有 4 个实体共用同一个 id
   （`CLASS-ENTITY-WITHOUT-VALUE-EQUALITY`），所以「唯一 id 数」比「条数」少。
   基线 16 条（15 个唯一 id），当前 23 条（17 个唯一 id）——
   多出来的是这一轮新接入的 `class_shape` 通用探针
   （6 条实体缺值相等 + 1 条封装泄漏），是**新增覆盖**，不是回归。
   比对时用 id 集合差集看，不要只比数量。

2. **唯一一处结论级差异是实测值**：`INDEX_UNASABLE_FOR_QUERY_PATTERN` 的
   title 里嵌了 EXPLAIN 的实测耗时（ms 与倍数），每次跑都不同。
   结论不变：EXPLAIN 显示走 Filter 全量过滤而非 Index Cond。

## 为什么放在仓库里

原来它们在 `/tmp` 里，重启就没了 —— 结论「通用化没回归」就变成一句
没法复核的话。放进仓库的成本是 44 KB，收益是这件事永远可查。
