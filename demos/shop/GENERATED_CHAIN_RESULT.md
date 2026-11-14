# 物化链实测结果：生成的候选自己驱动执行

日期：2026-11-15。全部数字来自实测，复现命令在末尾。

## 1. 补的是哪个洞

候选生成器（`pilot/engine/emergence_candidates.py`）此前**不产生任何新东西**：

- 它提出的条件是 `data:table:shop_customer` —— 一个**空槽位**，没有值
- 具体值 `{"level": "VIP"}` 来自人写的注入目录 `emergence_injections.json`
- 执行器 `emergence_candidate_run.py:107` 是 `for chain in catalog.chains:` ——
  **真跑的是人写的链**，候选只在跑完之后拿来对一下「槽位种类重合没有」

所以「自动生成的候选链能自己抓出涌现缺陷」这句话当时**没有证据**。
那个「召回 1/1」量的是槽位种类重叠，不是独立发现。

## 2. 现在怎么跑

新增 `emergence_generated_run.py`，遍历**候选**而不是人写的链。取值来源：

| 条件种类 | 值从哪来 | 是否机器取得 |
|---|---|---|
| `data` | 库里该列的 `SELECT DISTINCT`（种子行） | 是 |
| `instance` | 项目声明的装配实例清单 | 是（清单是配置） |
| `request` | `value_sources.json` 声明的取值域 | **否，仍是人给的** |

模板 `scenario_template.json` 只说明「怎么下单、基线是什么」：
普通客户 / default 装配 / CNY。**没有 VIP、没有 vip、没有 EUR** —— 那些值由物化器枚举出来。

注入目录只提供连接信息（`instances` / `database`），它的 `chains` 一行都不读。

## 3. 适用域：枚举会把观测推出判据的适用范围

判据 `BI-QUOTA-LEDGER-PARITY` 声明了 `when: currency == CNY`。
第一版物化器把 currency 也枚举成 EUR，结果：

```
合起来 {VIP, vip, EUR} -> 判据不适用（ok=None, checked=0）
旧代码把它压成 violated=False, status=ok
裁决 = FALSIFIED_CHAIN（「合起来也不违反」）—— 假的
```

两处都修了（决议 D18）：

1. **物化器**：判据的 `when` 就是适用域声明，被 `eq` 约束的字段钉在域内值上，
   **连基线一起钉**（否则基线观测就不适用）。列别名（`customer_level` vs `level`）
   由取值源 `read_as` 声明；钉不上的约束进 `unmatched_pins` 告警。
2. **项目侧判据**：`ok is None` 一律 `status=not_applicable`，
   交给引擎 D17 的 UNDECIDABLE 闸门。「不适用」永远不许变成「没违反」。

## 4. 实测结果

```
取值域：
  data     shop_customer   ['NORMAL', 'VIP']  基线 NORMAL
  instance                 ['default', 'vip'] 基线 default
  request  currency        ['CNY', 'EUR']     基线 CNY

[C0000] 判据 BI-QUOTA-LEDGER-PARITY
    钉在适用域：currency = CNY
    注入 2 条：GEN-INSTANCE-VIP / GEN-DATA-SHOP-CUSTOMER-LEVEL-VIP
    裁决 = EMERGENT_CONJUNCTIVE
    核心集 = GEN-INSTANCE-VIP + GEN-DATA-SHOP-CUSTOMER-LEVEL-VIP

[C0001] 判据 BI-VIP-MEMBER-PRICE
    钉在适用域：customer_level = VIP、currency = CNY
    注入 1 条：GEN-INSTANCE-VIP
    裁决 = FALSIFIED_CHAIN（基线即违反 —— 违反不是注入造成的）
```

**两条候选，一条判涌现、一条判非涌现，都没看答案。**
C0001 的结论是对的：会员价缺陷是单点缺陷（VIP 客户在标准装配下就拿不到折扣），
不是涌现。生成器自己把这两类分开了。

## 5. 顺带挖出并修掉的三个洞

| 洞 | 实测证据 | 修法 |
|---|---|---|
| 项目侧判据把「不适用」压成「没违反」 | 注入 EUR 后判出假 `FALSIFIED_CHAIN` | `ok is None` → `not_applicable` |
| `db_query` 吞掉 psql 错误 | 外键挡住 DELETE，rc=1 / stdout 空 → 清理报「deleted 0」 | rc≠0 直接抛；文件层清理先删附属记录 |
| 跨仓库探针用种子客户下单不删 | 订单 1153（`demo-vip`）留在库里，**残留守卫按前缀查不到** | 按外键顺序自删，id 只接受纯数字 |

第三个洞是既有的，不是本轮引入 —— 但 V3 的外键让它多留了 2 条附属记录。

## 5b. 独立审查又挖出 8 处（2 HIGH + 6 MEDIUM）

上面三个洞是我自己撞出来的。把新代码交给独立审查 agent 后，它用最小复现又确认了 8 处 ——
**两条 HIGH 恰好打在我声称已修好的「零残留」上**：

- 跨仓库探针清理失败只打印，`main` 没有退出码 → **exit 0**；「200 但解析不出 id」被当成
  「没造数据」跳过清理，而 `@Transactional` 的 200 意味着订单已提交。
- 文件层把 `db_query` 的异常降格成 `deleted=-1` 写进产物，**没有任何检查消费它** ——
  我只做到「错误可见」，没做到「判红」。

其余六处：`URLError` 穿透绕过 UNDECIDABLE 闸门；落库校验跨两条序列化链比字符串
（`numeric(10,2)` 的 100.00 在 reader 侧是 `'100.0'`）会把已落库判成死注入进而翻转裁决；
`pins_from_when` 对缺 value 的 eq 子句 `KeyError`；钉值回写对缺 flow 块 `KeyError`；
注入 id 不含表名导致同列名同取值时第二条被静默丢掉。全部已修，逐条锁进测试。

**推回一条建议**：审查建议把 `cleanup_baselines` 挪进 `try`。那会制造更大的洞 ——
基线缺失时 `{max_id_before}` 退化成 `"0"`，SQL 变成 `DELETE FROM shop_order WHERE id > 0`，
**删光种子数据**。改成加固 `run_cleanup`（基线缺失拒绝执行）。

注入 id 现在含槽位（`GEN-DATA-SHOP-CUSTOMER-LEVEL-VIP`），所以裁决指纹与上一版不同；
两次连跑指纹一致 `6ed8e03ef8638a04`。手写链的 FDR 门禁分母哈希不受影响，仍逐字节不变。

## 6. 边界（不许当成已解决）

- **request 取值域仍是人声明的**。只有 data 值是机器从库里读的。
- **规模没证明**：2 条候选、每个取值域 2 个值。证明的是机制成立，不是规模能力。
- **活性检查更弱**：物化链用 `applied`（施加成功即活），不是手写的「单独注入就该有症状」。
  后者要知道答案，而且会把「单独 inert、合起来才起作用」的条件判成死注入 —— 那正是涌现。
  弱在哪里必须说出来：`applied` 证明条件到达了系统，不证明条件造成了什么。
- **物化链不进缺陷台账**：它的 `defect` 是占位键，不是已归属的根因。
- **残留守卫本身没改**：仍按 `ai_probe_` 前缀查，用种子客户的探针它看不见。
  本轮靠探针自删 + **清理失败判红**（退出码非 0）补上，守卫的这个盲区还在。

## 7. 复现

```bash
python3 -m pytest pilot/tests/test_emergence_materialize.py -q   # 28 条
python3 -m pytest pilot/tests/test_xrepo_cleanup.py -q           # 3 条
python3 -m pytest demos/shop/test_probe_three_state.py -q        # 4 条
python3 -m pytest demos/shop/test_probe_alive_and_transport.py -q # 6 条
python3 -m pytest pilot/tests/test_cleanup_must_fail_loud.py -q  # 13 条
python3 demos/shop/emergence_generated_run.py                    # 真跑
bash demos/shop/run_pipeline.sh                                  # 全量，exit 0
```

跑完库必须回到种子基线：**29 笔订单 / 0 条占用 / 0 条分录 / max id 923**。
