# layered-audit

细胞式分层代码审计。把 Java 仓库拆成**细胞**(函数 / 文件 / 类 / 服务 / 集成五层),从 **PRD 与代码采集**业务判据,再用图算法判定缺陷与细胞间的相互破坏。

**核心特点是拒绝猜**:每条结论都能回原文;查不到就报 `unknown`,而不是「没问题」。

```
退出码 0 = 全绿    1 = 有红    2 = 没查完(INCOMPLETE)
```

`2` 与 `0` 严格区分 —— **「没查完」不等于「查了没问题」**。

---

## 前提

| 需要 | 说明 |
|---|---|
| Python **3.10+** | 引擎**零第三方依赖**,纯标准库 |
| **JDK**(`javap` 可用) | 读字节码用,**不需要 Maven/Gradle 跑构建** |
| 被测项目**已编译** | 需要 `target/classes` 或 `build/classes` 里的 `.class` |

测试需要 `pytest`(仅开发用):`pip install -r requirements-dev.txt`

## 安装

```bash
git clone https://github.com/joel-duqiong/layered-audit.git
cd layered-audit
pip install -e .          # 装出 layered-audit 命令
```

不想装也行,直接跑脚本:

```bash
python3 pilot/engine/audit_entry.py --repo /path/to/your-java-project --out /tmp/audit-out
```

## 一条命令

```bash
layered-audit --repo /path/to/your-java-project --out /tmp/audit-out
```

它会**自动**发现字节码、派生项目绑定(包前缀、模块根)、驱动上游生产者,然后产出四支柱报告:

```
[INCOMPLETE] oracle         118/177   判据采集与可执行映射
[INCOMPLETE] capability     139/177   细胞使命与能力图
[INCOMPLETE] architecture   347/610   架构禁止边
[INCOMPLETE] emergence    8385/148785 细胞间相互破坏(涌现)
退出码 2
```

**只读被测仓库**:`--out` 落在仓库内(含符号链接逃逸)会被拒绝。产物全部写到你指定的目录。

## 两项需要你供给的输入

工具会**明确报出**这两项缺口,不会静默跳过:

```bash
layered-audit --repo /path/to/project --out /tmp/out \
  --openapi  /path/to/openapi.json \
  --invariants /path/to/invariants.json \
  --docs /path/to/PRD.md /path/to/API.md
```

| 输入 | 为什么不可约 |
|---|---|
| `--openapi` | 真实接口契约。工具能从 mapping 注解**推导出入口绑定**,但那不含 schema;L2 接口契约判据仍需真实契约 |
| `--invariants` | **跨字段业务口径**(如「成交价不得高于套餐总价」)。工具能推导的只有**单字段技术约束**,两者人口不相交,推导不可替代人工业务声明 |

人工供给**永远优先于推导**,且**不与推导合并**(口径不同,合并会混淆)。

## 怎么读结果

- **`confirmed` 缺陷** → 进台账,每条带 `file:line` 与复现命令
- **`unconfirmed`(待确认疑点)** → 规范层风险,**需业务方确认**;既不是缺陷也不是已排除
- **`refuted`(已证伪)** → 文档明确否定该预期,**只保留观测**,不再等确认
- **`not_applicable`** → 本图未建模该细胞,**不是「已测无问题」**

三个口径**并排报、不混算**。报告里另有闸门覆盖清单:哪些生产者已接判据门禁、哪些未接、未接名下有多少条未裁定。

## 已知局限(不隐藏)

- **涌现裁决需要约束截面**:候选生成不需要业务 oracle,但判定一致性需要;无判据时全部 `unknown`,不伪造结论
- **建模域是声明的**:关系网只给 `*Controller` / `*Service` / `*Repository` 建方法节点,域外细胞报 `not_applicable`
- **高阶障碍**:当前算重叠图 1-骨架的 GF(2) 奇偶;跨资源环已支持,更高阶需另行扩展
- **新克隆有 4 条守卫跳过**:它们需要探针产物(`*_declaration.json` 等,已 gitignore),在带产物的工作区才跑

## 开发

```bash
pip install -r requirements-dev.txt
python3 -m pytest pilot/tests demos/shop -q
```

设计文档见 `DESIGN.md`;判据接入见 `RULE_ONBOARDING.md`;工程纪律与踩坑记录见 `LESSONS.md`。

## License

MIT