# 特性覆盖矩阵（特性 × 载体）

回答的是：这套审计在**框架运行时语义**上看得见什么、看不见什么。
跟 category 覆盖度矩阵是同一模式，只是第二根轴。

| 特性 | 检测器 | 覆盖它的载体 | 看不见它的载体 | 状态 | 受影响结论 |
|---|---|---|---|---|---|
| 跨仓库调用边界（HTTP / MQ / RPC） | `xrepo_probe.py` | integration.e2e_three_source | function.jvm_reflection, file.jvm_whole_class | partial | 2 条 |
| AOP 切面（代理织入） | `java_feature_scan.py` | integration.e2e_three_source | function.jvm_reflection, file.jvm_whole_class | partial | 2 条 |
| 依赖注入装配 | `spring_ctx_probe.py` | service.http_contract | function.jvm_reflection, file.jvm_whole_class | partial | 2 条 |
| 动态配置注入（@Value / 配置绑定 / 配置刷新） | `java_feature_scan.py` | — | function.jvm_reflection, file.jvm_whole_class, service.http_contract | blind | 4 条 |
| JPA 持久化（懒加载 / N+1 / 脏写） | `java_feature_scan.py` | class.dto_db_contract, service.http_contract | function.jvm_reflection, file.jvm_whole_class | partial | 2 条 |
| HTTP 契约（请求/响应形状 / 错误码） | `—` | service.http_contract | — | covered | 0 条 |

## 载体的盲区

### function.jvm_reflection（function 层 / java）


### file.jvm_whole_class（file 层 / java）


### class.dto_db_contract（class 层 / java）


### service.http_contract（service 层 / java）


### integration.e2e_three_source（integration 层 / java+python）

