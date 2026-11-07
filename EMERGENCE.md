# 集群与涌现：外部工作如何映射到本项目

> 这份文档回答两个问题：**我们要做的东西，别人做过吗？**（做过，骨架现成）
> **哪些能直接抄，哪些必须改造，哪些是过度设计不该抄。**
>
> 调研时间 2026-11-07。结论先行：
>
> **「每个函数/类/文件/服务一个 Agent + 关系网 + 静态/动态双命 + 改一处只动一处 +
> 个体对但集体错」这套东西，业界有成熟骨架**，而且我们已经有其中最贵的部分
> （层契约、oracle 绑定、差分出单、归属不重复开单、攻击链）。
> 缺的是**编排**：依赖拓扑、影响分析、按需调度、以及涌现的系统化方法论。
>
> **最重要的一条外部结论，直接命中我们的架构**：
> **单 Agent 的结论不预测群体的结论**（[MAEBE](https://arxiv.org/abs/2506.03053)）。
> 这不是 LLM 专属结论 —— 对我们的意思是：
> 「每个函数都测过了」**不能推出**「整个服务是对的」。
> 我们的攻击链（CHAIN-A..E）就是这条原理的最小实现，但只有 5 条，是手工挑的。

---

## 一、引用清单与映射

### 1.1 涌现性本身：单点结论不预测集体结论

| 来源 | 核心结论 | 对我们的映射 |
|---|---|---|
| [MAEBE: Multi-Agent Emergent Behavior Evaluation Framework (arXiv:2506.03053)](https://arxiv.org/abs/2506.03053) | 孤立单个体的安全/能力评估**不能**外推到群体；必须做群体级评估 | **我们已经有「集权层」这个层** —— 但它现在只有 5 条手写攻击链。应该升级成**从关系网自动生成候选链** + 群体级判定 |
| [MultiAgentBench](https://www.emergentmind.com/topics/multiagentbench) | 协调拓扑影响群体表现（graph-mesh 最好、tree 最弱）；**每个 Agent 的边际贡献随 Agent 总数上升而下降** | 给「Agent 数量」定上限的依据。我们契约里已写 `max_agents: 12` —— 现在没理由，纯拍脑袋；这里有依据 |
| [The Social Laboratory: Psychometric Framework for Multi-Agent LLM Evaluation (arXiv:2510.01295)](https://arxiv.org/abs/2510.01295) | 群体行为要用**心理测量式**量表评，不是看单次输出对不对 | 我们的「差分出单」就是量表思路的确定性版本 |
| [Diagnostics of cognitive failures in multi-agent expert systems (arXiv:2510.01295 姊妹篇)](https://arxiv.org/abs/2510.01295) | 对随机、多步、agentic 过程，**静态评估不适用**，要用动态评估协议 + 对处理上下文的**变异** | 「变异测试」在 agent 语境下 = 改一个 Agent 的输入/上下文，看群体结论是否变。我们的 `fn_dynamic.py --case` 是最弱版本 |
| [Towards Ethical MAS: A Mechanistic Interpretability Perspective](https://arxiv.org/abs/2512.04691) | 现有工作只记录**行为层面**的失效，不解释**为什么** | 我们的「归属规则 + 根因指纹」正是「为什么」的那一半 —— 这是我们比外部工作强的地方 |

### 1.2 关系网 → 级联预测：这是「涌现」最可操作的部分

| 来源 | 核心结论 | 对我们的映射 |
|---|---|---|
| [Model Discovery and Graph Simulation: A Lightweight Alternative to Chaos Engineering (arXiv:2506.11176)](https://arxiv.org/abs/2506.11176) | 从**真实 trace** 抽依赖图，再用 **Monte-Carlo 模拟**故障 → **不用跑昂贵的故障注入campaign**就能预测级联失败；**中心性预测 blast radius** | **直接可抄**：我们已有依赖图（229 条跨文件边 + 函数级调用边）。用它算「哪些节点是级联枢纽」，**优先跑那些攻击链** |
| [MicroFI (IEEE TDSC 2024)](https://doi.ieeecomputersociety.org/10.1109/TDSC.2024.3363902) | **优先化**故障注入：优先挑「最可能暴露缺陷」的注入点，而不是无差别注入 | 直接可抄：我们 5 条攻击链是手工挑的 → 应改成「按枢纽度自动排序 + 自动生成」 |
| [Chaos Engineering / 故障注入](https://en.wikipedia.org/wiki/Chaos_engineering) | 度量 **FDR（Fault Detection Rate）= 检出的注入故障 / 注入的故障总数** | 我们 HANDOFF §1 的成功标准第 4 条「漏检率有对抗性度量」—— **FDR 就是那个度量**，我们一直没实现 |
| [Lacuna: Feedback-based Automated Failure Testing of Microservice Applications](https://lacuna.tiptreesystems.com/work/feedback-based-automated-failure-testing-of-microservice) | 用**系统反馈**驱动故障注入，自动暴露微服务的错误处理缺陷 | 我们的 `concurrency_props.py` / `config_probe.py` 已经在做「注入异常场景 → 看系统反应」，但没系统化 |
| [Gleaner: Semantically-Rich Online Sampler for Microservice Diagnostics](https://arxiv.org/abs/2604.16810) | 按 trace 的异常分数加权采样（状态码错误 + 日志频次 + 延迟） | 我们的集成层观察可以按此加权，决定**跑哪些场景** |

**这一节的合并结论**：
**「关系网 → 枢纽 → 优先跑哪条链 → FDR 量化漏检率」是一条完整可落地的路径，
而我们已经有关系网和 5 条链，缺的只是「用图驱动」和「用 FDR 验收」。**

### 1.3 「每个单元一个 Agent + 关系网驱动编排」：这是「Agent 化」的现成骨架

| 来源 | 架构 | 对我们的映射 |
|---|---|---|
| [CodeWiki (FSoft-AI4Code, ACL 2026 Findings)](https://aclanthology.org/2026.findings-acl.288/) | 三段式：① AST 解析 → 归一化成 `depends_on` 有向图 G=(V,E)；② **递归多 Agent 文档生成 + dynamic delegation**（超 context 能力的模块派生子 Agent，子模块结果向上聚合）；③ 组装。**关键洞察：「A depends_on B」意味着 B 必须先被理解，所以拓扑排序决定处理顺序** | **这就是你说的「有关系才调用，没关系不调用」的算法形式**。我们已有 `cross_file_edges`，缺的是**拓扑序 + 按序委派** |
| [Understand Anything (Egonex-AI, 91k stars)](https://github.com/Egonex-AI/Understand-Anything) | 多 Agent 流水线把**每个文件、函数、类、依赖**映射进可交互知识图谱，含**自然语言职责摘要**、**diff 影响分析** | **「每个函数是干什么的」+「改一处影响哪里」** —— 你要的两样，业界有 91k star 的实现 |
| [ArchDoc Generator (zread.ai)](https://zread.ai/techdebtgpt/architecture-doc-generator) | **11 个专职 AI Agent** + LangGraph 状态机编排：管**执行顺序、token 预算、迭代质量优化**；产物含 C4 模型图 | 我们的 `layers.yaml` 里的 oracle 绑定就是「专职分工」；token 预算（`max_tokens_per_round: 2M`）是同一个机制，我们写了没用 |
| [Cartographer](https://lobehub.com/skills/phrazzld-codex-config-cartographer) | 并行 AI 子 Agent → `CODEBASE_MAP.md`（架构图 + **每文件用途** + 依赖图）；**token 预算化任务切分**（~150K tokens/子 agent），Opus 编排 + Sonnet 子 agent | 同上；且明确「**token 预算化**」是能跑通大仓的关键 |
| [Agent4cs (arXiv:2607.01425)](https://arxiv.org/abs/2607.01425) | 仓库级摘要分解成子任务分给不同 Agent，在大型分层代码库上比单模型工具**好 22–34%** | 量化了「分工」的收益 —— 对应我们 B4 编排化的收益预期 |

### 1.4 我们比外部工作强的地方（别丢）

| 我们的能力 | 外部现状 |
|---|---|
| **归属规则：owner = 能复现的最低层，高层只引用不重复开单** | 外部多智能体工作基本没有「谁负责修」的概念 |
| **确定性差分出单**（静态声明 vs 动态实测，两边互相独立） | 主流是 LLM 评审 + benchmark 打分，不可回归 |
| **有基线的门禁 + 回归资产**（缺陷修好 → 用例 XPASS 强制摘标记） | benchmark 是静态的，修好了不会「红」 |
| **每条结论带可跑复现命令**，14 条不可执行的已降到 3 条 | 多数 agent 评测不可复现 |

---

## 二、逐条落地：直接采用 / 需改造 / 不采用

### 2.1 直接采用（不引模型、不改判定口径，立刻可做）

| # | 做法 | 出处 | 我们的落点 |
|---|---|---|---|
| **D1** | **依赖拓扑排序**：按 `depends_on` 图排 Agent 处理顺序 | CodeWiki | `engine/change_impact.py` |
| **D2** | **变更影响分析**：git diff → 沿边反向传播 → 算出受影响靶区 | Understand Anything 的 diff impact | 同上 |
| **D3** | **按影响集调度**：没被波及的 Agent 复用上次结论 | CodeWiki 拓扑序 + 我们 `max_agents` | `run_pipeline.sh` |
| **D4** | **枢纽度驱动的攻击链排序**：用中心性给攻击链排优先级 | Model Discovery / MicroFI | `engine/attack_chain_priority.py` |
| **D5** | **FDR 验收**：注入 N 个已知缺陷，看我们检出多少 | Chaos engineering 度量 | 我们已有 5 条已知链 + pytest xfail 资产 → 可直接算 |

### 2.2 需要改造（要引模型，会动确定性与成本）

| # | 做法 | 出处 | 改造要点 |
|---|---|---|---|
| **M1** | **语义地图**：每个函数/类/文件/服务的自然语言职责 | Understand Anything / ArchDoc | **不能直接进结论**。它只能生成「待验证线索」，仍要过确定性层验证（差分出单）。否则门禁失去可复现性 |
| **M2** | **递归委派**：超 context 能力的模块派生子 Agent | CodeWiki dynamic delegation | 我们函数层 461 个函数已经超单 context → 需要分组委派策略 |
| **M3** | **token 预算调度** | ArchDoc / Cartographer | 契约里已写 `max_tokens_per_round: 2M`，现在完全没用上 |
| **M4** | **上下文变异测试**：改一个 Agent 的上下文，看群体结论变不变 | Diagnostics of cognitive failures | 我们 `--case` 重放是最弱版本；变异版能测「结论对输入的敏感性」 |

### 2.3 不采用（会过度设计）

| 做法 | 为什么不采用 |
|---|---|
| 完整 LangGraph 状态机编排器 | 我们只有 9 个 agent、5 层，状态机是杀鸡用牛刀；用拓扑序 + 简单依赖解析就够（CodeWiki 的核心也只是一个拓扑排序） |
| 心理测量量表式的群体能力评分 | 我们要的是「有没有缺陷」，不是「群体能力多强」 |
| 通用 benchmark / 排行榜 | 我们要的是**门禁**（回归即红），不是刷分 |

---

## 三、目标架构（把上面的东西拼起来）

```
        ┌──────────────────── 代码仓库 ────────────────────┐
        │                                                      │
        │   依赖图 G=(V,E)      ← javap/AST 抽，跨文件+跨类    │
        │   V = 函数/类/文件/服务/对象                          │
        │        │                                             │
        │        ├─ 拓扑序（谁必须先被理解）                     │
        │        │                                             │
        │   ┌────▼─────────────────────────────┐              │
        │   │  Agent 群（每层/每单元一个）        │              │
        │   │  静态命：javap/DDL/OpenAPI/前端解析 │              │
        │   │  动态命：JVM直调/真实容器/DB实况    │              │
        │   │  判定：oracle 绑定 + 差分出单        │              │
        │   └────┬─────────────────────────────┘              │
        │        │ 只调被波及的（D2/D3）                        │
        │        ▼                                             │
        │   根因归并 + 归属（不重复开单）                       │
        │        │                                             │
        │        ▼                                             │
        │   涌现层：个体对 → 群体错？                          │
        │        ├─ 攻击链（手工 5 条 → D4 按枢纽自动排序）      │
        │        └─ 跨仓库 Agent 群（两群互相调用）              │
        │                                                      │
        └──────────────────────────────────────────────────────┘
                     │
                     ▼
          门禁基线 + FDR 验收（D5）
```

**跨仓库**：一个仓库 = 一群 Agent；两个仓库 = 两群 Agent，
它们之间有**服务级调用边**（HTTP/RPC）。跨仓库的「涌现」= A 仓库的 Agent 调用
B 仓库的 Agent 时组合出来的后果。**这是我们 attack_chain 里 CHAIN-A/B 那种链的
跨仓库版本**，机制现成。

---

## 四、还没解决的诚实问题

1. **「每个函数一个 Agent」的粒度是否过细？** 函数层已有 461 个函数（一个类里就有）。
   CodeWiki 的做法是**按模块递归委派**，不是按函数建 Agent —— 每个函数一个 Agent
   的 token 成本可能不现实。**建议：函数是 Agent 的「感知单元」，Agent 按模块/类分组。**
2. **语义地图（M1）会让门禁失去可复现性吗？** 会 —— 所以必须双轨：
   确定性层出结论（可钉基线），模型层出线索（要验证）。
3. **FDR 的分母怎么定？** 我们需要「已知缺陷全集」才能算漏检率。
   目前只有 5 条攻击链 + 现有 68 条根因。**这是要先补的。**

---

## 五、引用

- MAEBE: Multi-Agent Emergent Behavior Evaluation Framework — https://arxiv.org/abs/2506.03053
- Model Discovery and Graph Simulation: A Lightweight Alternative to Chaos Engineering — https://arxiv.org/abs/2506.11176
- The Social Laboratory: A Psychometric Framework for Multi-Agent LLM Evaluation — https://arxiv.org/abs/2510.01295
- Diagnostics of cognitive failures in multi-agent expert systems — https://arxiv.org/abs/2510.01295
- Towards Ethical Multi-Agent Systems: A Mechanistic Interpretability Perspective — https://arxiv.org/abs/2512.04691
- Agent4cs: Multi-Agent AI System Solves Code Summarization — https://arxiv.org/abs/2607.01425
- MultiAgentBench — https://www.emergentmind.com/topics/multiagentbench
- Gleaner: Semantically-Rich and Efficient Online Sampler for Microservice Diagnostics — https://arxiv.org/abs/2604.16810
- CodeWiki (FSoft-AI4Code, ACL 2026 Findings) — https://aclanthology.org/2026.findings-acl.288/
- Understand Anything — https://github.com/Egonex-AI/Understand-Anything
- ArchDoc Generator — https://zread.ai/techdebtgpt/architecture-doc-generator
- MicroFI: Non-Intrusive and Prioritized Request-Level Fault Injection — https://doi.ieeecomputersociety.org/10.1109/TDSC.2024.3363902
- Lacuna: Feedback-based Automated Failure Testing of Microservice-based Applications — https://lacuna.tiptreesystems.com/work/feedback-based-automated-failure-testing-of-microservice
