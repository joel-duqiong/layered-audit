# 特性覆盖矩阵（特性 × 载体）

回答的是：这套审计在**框架运行时语义**上看得见什么、看不见什么。
跟 category 覆盖度矩阵是同一模式，只是第二根轴。

| 特性 | 检测器 | 覆盖它的载体 | 看不见它的载体 | 状态 | 受影响结论 |
|---|---|---|---|---|---|
| AOP 切面（代理织入） | `java_feature_scan.py` | service.http_contract, integration.e2e_three_source | function.jvm_reflection, file.jvm_whole_class | partial | 7 条 |
| 依赖注入装配 | `spring_ctx_probe.py` | service.http_contract | function.jvm_reflection, file.jvm_whole_class | partial | 7 条 |
| RPC 边界（Dubbo / gRPC / Feign / MQ） | `java_feature_scan.py` | — | service.http_contract | blind | 5 条 |
| 动态配置与运行时行为变更 | `property_binding_probe.py` | service.http_contract | function.jvm_reflection, file.jvm_whole_class | partial | 7 条 |
| JPA 持久化语义（懒加载 / 脏写 / 查询放大） | `java_feature_scan.py` | class.dto_db_contract, service.http_contract | function.jvm_reflection, file.jvm_whole_class | partial | 7 条 |
| HTTP 契约与错误码 | `—` | service.http_contract | — | covered | 0 条 |

## 载体的盲区

### function.jvm_reflection（function 层 / java）

- AOP 切面语义（@Transactional / @Async / @Cacheable / @PreAuthorize）—— 反射直调不走 Spring 容器，代理根本不参与
- Spring 依赖注入装配结果（@Primary / @Qualifier / @Profile / @Conditional 的选择）—— 仓库被打桩，看到的是假依赖图
- Spring 代理行为（自调用失效、private/final/static 方法不被织入）
- 配置注入（@Value）与配置刷新 —— 容器不启动，配置不参与
- 任何依赖持久化或容器生命周期的语义

### file.jvm_whole_class（file 层 / java）

- AOP 切面语义 —— 容器外实例化，代理不参与
- 依赖注入装配结果 —— 依赖是打桩的
- 跨文件的 ORM 交互（懒加载 / N+1 / 脏写）

### class.dto_db_contract（class 层 / java）

- 跨实体的关联加载行为（懒加载时机、N+1）
- 事务隔离级别下的可见性

### service.http_contract（service 层 / java）

- 非 HTTP 入口（Dubbo / gRPC / MQ）—— 本项目没有，但下一个项目有的话这里就是盲区
- 网络层真实超时/熔断/降级（探针在同机同网，测不出真实网络故障）

### integration.e2e_three_source（integration 层 / java+js）

- 前端渲染后的最终视觉（本系统只到视图模型，不到像素）
- 浏览器端状态与本地存储
