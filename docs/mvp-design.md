# 校园活动管理系统 — MVP 设计方案（V1.0 历史文档）

> 版本：v0.2　最后更新：2026-09-18
>
> ⚠️ **本文档描述的是 V1.0**。系统已经迭代到 **V2.0**：角色改为「学生 /
> 活动组织教师 / 系统管理员」，报名扩展为五态并加入**候补队列**与**报名审核**，
> 活动审核流（提交 → 管理员通过 / 驳回）已移除（改为教师直接发布 + 管理员下线监督）。
> V2.0 的实现依据见 [`v2.0-design.md`](./v2.0-design.md)与《实验2-需求分析.md》。
> 本文档保留作为 V1.0 的历史记录；其中与 V2.0 冲突之处，以 V2.0 文档为准。
>
> 本文档是**技术实现层面**的依据：表结构、接口清单、并发方案、技术选型、里程碑。
> **产品层面**的设计（背景、用户画像、功能规则、界面、验收标准）见
> [`product-design.md`](./product-design.md)。
> 两份文档中，业务规则以产品设计文档为准，实现细节以本文档为准；范围变更须同时更新。

## 1. 项目概述

面向校园场景的活动发布与报名管理系统。核心目标是跑通一条完整闭环：

**组织者发布活动 → 学生浏览并报名 → 现场签到 → 数据统计与复盘**

只要这条闭环断掉，功能再多也不算 MVP 交付。

### 1.1 已确认的前置决策

| 决策项 | 结论 |
| --- | --- |
| 端形态 | 先做 Web 端（响应式，兼顾手机浏览器） |
| 发布权限 | 校级管理员可直接发布；社团发布的活动须经校级管理员审核 |
| 统一身份认证 | 暂不对接（CAS / 企业微信 / 钉钉） |
| 技术栈 | 无外部约束，由本文档按开发者背景推荐（见第 8 节） |

## 2. 角色与权限

三个角色：**学生（STUDENT）**、**社团（CLUB）**、**校级管理员（ADMIN）**。

### 2.1 权限矩阵

| 操作 | 学生 | 社团 | 管理员 |
| --- | :---: | :---: | :---: |
| 浏览活动列表 / 详情 | ✓ | ✓ | ✓ |
| 报名 / 取消报名 | ✓ | ✓ | ✓ |
| 签到（本人） | ✓ | ✓ | ✓ |
| 创建活动 | ✗ | ✓（→ 待审核） | ✓（→ 直接发布） |
| 编辑活动 | ✗ | 仅本人创建且未发布 | 全部 |
| 提交审核 | ✗ | ✓ | — |
| 审核通过 / 驳回 | ✗ | ✗ | ✓ |
| 取消活动 | ✗ | 仅本人创建 | 全部 |
| 查看报名名单 | ✗ | 仅本人创建的活动 | 全部 |
| 核验签到（手动勾选 / 看签到码） | ✗ | 仅本人创建的活动 | 全部 |
| 查看统计 | ✗ | 仅本人创建的活动 | 全局 |
| 分类管理 | ✗ | ✗ | ✓ |
| 用户与角色管理 | ✗ | ✗ | ✓ |

### 2.2 权限实现要求

- 每个写接口**必须在服务端校验归属权与角色**，前端隐藏按钮不构成权限控制。
- 归属权校验统一走一个中间件 / 拦截器，避免散落在各个 Controller 里漏掉。
- 社团账户通过 `user.club_name` 标识所属社团；MVP 不建独立的社团表。

## 3. 活动状态机

```
草稿 DRAFT ──提交──> 待审核 PENDING ──通过──> 已发布 PUBLISHED ──开始──> 进行中 ONGOING ──结束──> 已结束 FINISHED
                  │                    │
                  └──驳回──> 已驳回 REJECTED（可修改后重新提交）
                                       │
                       未结束前任意阶段 ──> 已取消 CANCELED
```

管理员创建的活动跳过 `PENDING`，直接置为 `PUBLISHED`。

| 状态 | 说明 | 可见性 | 允许的操作 |
| --- | --- | --- | --- |
| `DRAFT` | 草稿 | 仅创建者 | 编辑、删除、提交审核 |
| `PENDING` | 待审核 | 创建者 + 管理员 | 管理员通过 / 驳回；创建者可撤回 |
| `REJECTED` | 已驳回 | 创建者 + 管理员 | 创建者修改后重新提交 |
| `PUBLISHED` | 已发布，报名中 | 全员 | 学生报名 / 取消报名；创建者编辑信息 |
| `ONGOING` | 进行中，签到开放 | 全员 | 签到；创建者手动核验 |
| `FINISHED` | 已结束，数据归档 | 全员（只读） | 查看统计 |
| `CANCELED` | 已取消 | 全员（只读） | — |

> **为什么先定枚举**：把「草稿」和「已发布」混在一个布尔字段里是这类系统最常见的返工来源。状态一旦成为枚举，后续加审核节点只是加一个值，不用改存量数据。

## 4. 功能范围

### 4.1 MVP 必做

1. **登录与角色权限** — 学号 / 工号 + 密码登录，服务端签名 session 保存登录态（Cookie），三角色权限拦截。**已完成**，含安全基线（登录失败锁定、CSRF、会话安全、生产配置自检）。
2. **活动创建与发布** — 字段：标题、封面图、简介（纯文本）、分类、开始 / 结束时间、地点、人数上限、报名截止时间。管理员直接发布，社团提交后进审核。
3. **活动审核流** — 管理员对待审核活动进行通过 / 驳回，驳回须填原因；操作记入审核日志。
4. **活动列表与详情** — 按时间、分类筛选 + 标题关键词搜索；分页；详情页含报名入口。
5. **在线报名与取消** — 一键报名 / 取消，校验名额、报名截止时间、重复报名。
6. **签到核验** — 两种方式：学生输入 6 位动态签到码；或组织者在名单上手动勾选。
7. **报名与签到统计** — 报名人数、实到人数、签到率、名单导出 CSV。

### 4.2 V1.1 再做

- 候补队列与自动递补
- 站内通知与提醒（活动被驳回、报名成功、活动临近）
- 复杂报表与图表汇总
- 活动二维码海报生成

### 4.3 明确不做

- 学分 / 积分体系（涉及与教务系统对接，是另一个项目）
- 收费与在线支付
- 评论、私信、点赞等社交互动
- 蓝牙 / NFC / 人脸签到
- 富文本编辑器
- 多校区 / 多租户

## 5. 数据库设计

数据库：MySQL 8，字符集 `utf8mb4`，时间统一以 **UTC** 存储。

> **MVP 阶段实际先用 SQLite**，但表结构按 MySQL 8 的口径设计，迁移时注意两处差异：
> 自增主键在 SQLite 下为 `INTEGER PRIMARY KEY`、在 MySQL 下为 `BIGINT AUTO_INCREMENT`（SQLAlchemy 中统一写成 `Integer/BigInteger` + `autoincrement=True` 即可兼容）；
> `DATETIME` 类型由 SQLAlchemy 的 `DateTime` 负责映射，SQLite 侧无原生时间类型。
> 用 SQLAlchemy 定义模型（而不是裸 SQL 建表）可以让这次切换几乎零成本——这也是选 ORM 而非手写 SQL 的主要理由之一。
> 唯一索引、外键等约束两者语义一致，可以照常使用。

### 5.1 user — 用户

| 字段 | 类型 | 约束 / 说明 |
| --- | --- | --- |
| id | BIGINT | PK, AUTO_INCREMENT |
| student_no | VARCHAR(20) | UNIQUE, 登录账号 |
| name | VARCHAR(50) | 姓名 |
| password_hash | VARCHAR(100) | bcrypt |
| email | VARCHAR(100) | |
| phone | VARCHAR(20) | 可空 |
| role | VARCHAR(20) | `STUDENT` / `CLUB` / `ADMIN` |
| club_name | VARCHAR(100) | 仅 CLUB 角色使用 |
| status | TINYINT | 1 正常 / 0 禁用 |
| created_at / updated_at | DATETIME | |

索引：`uk_student_no (student_no)`、`idx_role (role)`

### 5.2 category — 活动分类

| 字段 | 类型 | 约束 / 说明 |
| --- | --- | --- |
| id | BIGINT | PK |
| name | VARCHAR(30) | UNIQUE，如讲座 / 文体 / 志愿 / 竞赛 |
| sort_order | INT | 排序 |
| created_at | DATETIME | |

### 5.3 activity — 活动

| 字段 | 类型 | 约束 / 说明 |
| --- | --- | --- |
| id | BIGINT | PK |
| title | VARCHAR(100) | |
| cover_url | VARCHAR(255) | |
| description | TEXT | 纯文本 |
| category_id | BIGINT | FK → category.id |
| organizer_id | BIGINT | FK → user.id，创建人 |
| start_time / end_time | DATETIME | UTC |
| location | VARCHAR(150) | |
| capacity | INT | 人数上限，> 0 |
| enrolled_count | INT | 默认 0，**冗余计数，用于原子扣减** |
| enroll_deadline | DATETIME | 报名截止，UTC |
| status | VARCHAR(20) | 见第 3 节状态机 |
| reject_reason | VARCHAR(255) | 驳回原因 |
| approved_by | BIGINT | FK → user.id |
| approved_at | DATETIME | |
| check_in_code | VARCHAR(6) | 开启签到时生成的 6 位码 |
| created_at / updated_at | DATETIME | |

索引：`idx_status_start (status, start_time)`、`idx_organizer (organizer_id)`、`idx_category (category_id)`

> `enrolled_count` 是刻意引入的冗余字段。它不是"缓存"，而是并发控制的载体——没有它就只能靠「先查再插」，那必然超卖。

### 5.4 enrollment — 报名记录

| 字段 | 类型 | 约束 / 说明 |
| --- | --- | --- |
| id | BIGINT | PK |
| activity_id | BIGINT | FK → activity.id |
| user_id | BIGINT | FK → user.id |
| status | VARCHAR(20) | `ENROLLED` / `CANCELED` |
| enrolled_at | DATETIME | |
| canceled_at | DATETIME | |
| check_in_at | DATETIME | 可空，签到时间 |
| check_in_by | BIGINT | 可空，FK → user.id，手动核验人 |

约束与索引：

- `uk_activity_user (activity_id, user_id)` — **唯一索引，防重复报名，是最后一道防线**
- `idx_activity_status (activity_id, status)`

> 签到信息直接挂在报名记录上，不单独建表。MVP 阶段不支持"未报名直接签到"，因此一对一，合表更简单。若将来放开现场报名，再拆出独立的 `attendance` 表。

### 5.5 audit_log — 审核日志

| 字段 | 类型 | 约束 / 说明 |
| --- | --- | --- |
| id | BIGINT | PK |
| activity_id | BIGINT | FK → activity.id |
| operator_id | BIGINT | FK → user.id |
| action | VARCHAR(20) | `SUBMIT` / `APPROVE` / `REJECT` / `CANCEL` |
| reason | VARCHAR(255) | 驳回或取消原因 |
| created_at | DATETIME | |

## 6. 关键技术方案

### 6.1 报名并发控制（最高优先级风险）

限额 100 人的活动，在并发下变成 103 人，是这类系统的经典缺陷。**禁止使用「先查询人数、再插入」的写法**——检查与写入之间永远存在竞态窗口。

正确做法是让数据库在一次原子操作里完成「判断 + 扣减」：

```sql
START TRANSACTION;

-- 步骤 1：原子扣减名额。影响行数为 0 即表示失败，直接回滚
UPDATE activity
   SET enrolled_count = enrolled_count + 1
 WHERE id = ?
   AND status = 'PUBLISHED'
   AND enroll_deadline > NOW()
   AND enrolled_count < capacity;

-- 步骤 2：写入报名记录。唯一索引冲突则回滚，返回「你已报名」
INSERT INTO enrollment (activity_id, user_id, status, enrolled_at)
VALUES (?, ?, 'ENROLLED', NOW());

COMMIT;
```

取消报名时反向扣减，且必须防止计数为负：

```sql
UPDATE enrollment
   SET status = 'CANCELED', canceled_at = NOW()
 WHERE activity_id = ? AND user_id = ? AND status = 'ENROLLED';
-- 仅当影响行数为 1 时，才执行下面的计数回退
UPDATE activity
   SET enrolled_count = enrolled_count - 1
 WHERE id = ? AND enrolled_count > 0;
```

配套规则：

- **距报名截止不足 24 小时起，名单锁定，不可取消**（详见 `product-design.md` 第 5.3 节）。
- **已签到的报名不可取消**（避免统计口径混乱）。
- 活动进入 `ONGOING` 后不可取消报名。
- `enrolled_count` 与报名记录数应定期对账，提供一条校验 SQL 用于排查。

### 6.2 时间与时区

- 数据库统一存 UTC，展示层按浏览器本地时区转换。
- 报名截止、活动开始等**所有边界判断以服务端时间为准**，前端传入的时间一律不可信。
- 时间比较统一使用数据库的 `NOW()`（同一时区上下文），避免应用服务器与数据库时钟/时区不一致。

### 6.3 签到码

- 活动进入 `ONGOING` 时由服务端生成 6 位数字码，写入 `activity.check_in_code`。
- 校验时同时确认：活动处于 `ONGOING`、码值匹配、该用户存在 `ENROLLED` 的报名记录。
- 码在活动结束后自动失效（状态流转即失效，无需额外清理任务）。

## 7. 接口设计（草案）

> **实现说明（配合第 8 节的服务端渲染方案）**：路由分两类。
> **页面路由**返回渲染好的 HTML（如 `GET /activities` 是列表页、`GET /activities/<id>` 是详情页）；
> **动作路由**接收表单 POST，处理完成后重定向回页面，即 Post/Redirect/Get 模式。
> 下表列出的动作路径可直接复用，页面路由在此基础上补充即可。
> 鉴权使用 Flask 内置的签名 session（Cookie），比 JWT 简单且完全够用；等将来真正做前后端分离时再换成 token。

### 认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET / POST | `/auth/login` | 登录，建立会话 |
| POST | `/auth/logout` | 登出，清空会话 |
| GET | `/auth/me` | 当前登录用户（JSON） |
| GET / POST | `/auth/forgot-password` | 提交密码找回申请（公开） |
| GET / POST | `/auth/change-password` | 本人修改密码 |

### 活动

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/activities` | 公开 | 列表，支持 status / category / keyword / page 参数 |
| GET | `/activities/{id}` | 公开 | 详情 |
| POST | `/activities` | CLUB / ADMIN | 创建（CLUB → PENDING，ADMIN → PUBLISHED） |
| PUT | `/activities/{id}` | 创建者 / ADMIN | 编辑 |
| POST | `/activities/{id}/submit` | 创建者 | 提交审核 |
| POST | `/activities/{id}/approve` | ADMIN | 审核通过 |
| POST | `/activities/{id}/reject` | ADMIN | 审核驳回，body 含 reason |
| POST | `/activities/{id}/cancel` | 创建者 / ADMIN | 取消活动 |
| GET | `/activities/mine` | 登录用户 | 我创建的 / 我报名的 |

### 报名与签到

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/activities/{id}/enroll` | 登录用户 | 报名 |
| DELETE | `/activities/{id}/enroll` | 登录用户 | 取消报名 |
| GET | `/activities/{id}/enrollments` | 创建者 / ADMIN | 报名名单 |
| GET | `/activities/{id}/enrollments/export` | 创建者 / ADMIN | 导出 CSV |
| POST | `/activities/{id}/check-in/open` | 创建者 / ADMIN | 开启签到，返回签到码 |
| POST | `/activities/{id}/check-in` | 登录用户 | 学生凭码签到 |
| PUT | `/activities/{id}/enrollments/{userId}/check-in` | 创建者 / ADMIN | 手动勾选签到 |
| GET | `/activities/{id}/stats` | 创建者 / ADMIN | 报名数 / 实到数 / 签到率 |

### 分类与用户

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/categories` | 公开 | 分类列表 |
| POST / PUT / DELETE | `/categories/{id}` | ADMIN | 分类增改删 |
| GET | `/admin/` | ADMIN | 管理后台首页 |
| GET | `/admin/activities/pending` | ADMIN | 待审核列表 |
| GET / PUT | `/admin/users/{id}` | ADMIN | 用户查询与角色调整 |
| GET | `/admin/password-resets` | ADMIN | 密码找回申请队列 |
| POST | `/admin/password-resets/{id}/resolve` | ADMIN | 重置为随机临时密码（临时密码仅在本次响应中显示一次） |
| POST | `/admin/password-resets/{id}/reject` | ADMIN | 标记为未通过 |

## 8. 技术栈建议

### 8.1 开发者技术背景（2026-09-17 确认）

大二，目前只系统学过 C 语言，无 Web 开发经验。

因此选型的第一原则不是「主流」或「就业友好」，而是**同一时间只引入一个新变量**。跨度过大的技术栈会把时间耗在环境配置和框架概念上，而不是业务本身——这是学生项目最常见的失败原因。

### 8.2 已确认采用：Python 全栈（服务端渲染）

| 层次 | 技术 | 说明 |
| --- | --- | --- |
| 浏览器端 | HTML + CSS + Bootstrap | 无需构建步骤，不引入 npm |
| Web 框架 | Flask + Jinja2 | 路由 + 服务端模板渲染 |
| 数据访问 | SQLAlchemy ORM | 用 Python 对象读写数据库 |
| 数据库 | SQLite | 单文件、零配置，后续可平滑切换 MySQL |
| 测试 | pytest | 并发报名用 threading + barrier 模拟 |

选择理由：

1. **只新增一门语言**（Python），而不是 Java + JavaScript 两门同时上。
2. **没有前端构建步骤**——不引入 npm、打包工具与 node_modules。
3. **服务端渲染**：HTML 由服务器拼装后返回，因而完全不涉及前后端分离、接口联调、跨域这一类额外复杂度。
4. SQLite 无需安装数据库服务，备份即复制文件。

从 C 迁移到 Python 需要适应的只有四点：动态类型、缩进即语法、用 `dict` / `list` 代替 `struct` + 数组、不再需要手动管理内存。循环、条件、函数、数组等思维可直接复用。

### 8.3 备选（未采用）：Go + 标准库

若开发者偏好 C 那样的静态类型与编译手感，可选 Go：`net/http` + `html/template` + `database/sql` + SQLite，同样没有前端构建步骤。代价是需新学的项数略多，模板写法也比 Jinja2 啰嗦。

### 8.4 暂不建议：Java + Spring Boot + Vue

需同时新学 Java 语法、Maven、Spring 注解体系、Spring Security、MyBatis-Plus、MySQL、Vue 3、Vite、npm 与 Axios、前端路由与状态管理，约 9 项互相耦合的新概念。作为第一版实现的风险过高，可在 MVP 交付后再作为进阶方向。

> 若课程或比赛指定了技术栈，一律以指定为准，本节作废。

## 9. 开发里程碑

每个里程碑至少对应一个 commit，提交信息遵循 `type(scope): 描述`。

| 里程碑 | 内容 | 交付判据 |
| --- | --- | --- |
| **M0** ✅ | 项目脚手架（Flask 应用骨架、SQLAlchemy 模型、数据库初始化）、登录鉴权打通、安全基线 | 已完成：三角色可登录；越权被拒 |
| **M1** ✅ | 分类与活动 CRUD、活动列表 / 详情 | 已完成：管理员能建活动并在列表看到；筛选与分页可用 |
| **M2** ✅ | 状态机 + 审核流 + 角色权限拦截 | 已完成：社团提交 → 管理员审核 → 学生可见；非法流转被拒 |
| **M3** ✅ | 报名与取消（含并发控制） | 已完成：40 线程抢 25 名额，报名数恰好等于上限 |
| **M4** ✅ | 签到（签到码 + 手动核验） | 已完成：凭码签到成功，重复签到被拒，撤销可用 |
| **M5** ✅ | 统计与 CSV 导出 | 已完成：签到率口径正确，导出文件带 BOM |
| **M6** ✅ | 测试补齐、异常分支、联调 | 已完成：全部测试通过，覆盖第 6 节三个风险点 |

### 9.1 实现与本文档的差异（记录在案）

| 位置 | 差异 | 原因 |
| --- | --- | --- |
| 第 5 节 | 实际共 **6 张表**，多出 `password_reset_request` | 「忘记密码」在 2026-09-18 确认为 MVP 范围（见产品文档 F9） |
| 第 6.1 节 | 取消报名额外增加「**已签到的报名不可取消**」 | 已签到还能取消，签到率的分母会前后变化，统计口径自相矛盾 |
| 第 6.2 节 | 时间比较用应用侧的 `utcnow()`（Python），不是数据库的 `NOW()` | 全链路只存无时区的 UTC，两边取值必然一致；改用数据库 `NOW()` 反而要处理 SQLite 与 MySQL 的方言差异，收益为负 |
| 第 6.3 节 | 状态推进（`PUBLISHED → ONGOING → FINISHED`）在**每次请求的 before_request 里顺手同步**，没有独立定时任务 | MVP 单进程部署，引入调度器要多一整套运维依赖；状态完全由时间决定，读时现算不会算错 |
| 第 7 节 | 动作路由按 Post/Redirect/Get 实现；驳回、审核通过等入口额外接收 `next` 表单字段 | 从管理后台的队列里操作完，应当回到队列继续处理，而不是跳到单个活动的详情页 |
| 第 8.2 节 | 展示用时区用固定的 UTC+8，未引入 IANA 时区库 | 中国标准时间全年无夏令时，固定偏移足够；Windows 不自带 IANA 数据，为一个时区不值得加依赖 |

## 10. 测试要求

依据 `AGENTS.md`，每次改动后必须编写或更新测试，并在交付前确保全部通过。重点覆盖：

- **并发报名**：多线程同时报名，断言 `enrolled_count <= capacity` 且无重复记录
- **权限越权**：学生调用创建活动接口、社团编辑他人活动，均应返回 403
- **状态流转**：非法流转（如对 `FINISHED` 活动报名）必须被拒绝
- **时间边界**：报名截止前后各一秒的行为
- **重复操作**：重复报名、重复签到、重复取消

### 10.1 测试文件分布

| 文件 | 覆盖范围 |
| --- | --- |
| `tests/conftest.py` | 公共装置：应用 / 干净内存库 / 用户工厂 / 活动工厂 / 并发专用应用 |
| `tests/test_activities.py` | M1：创建与发布、表单校验、筛选分页、可见性、编辑与删除、状态按时间推进 |
| `tests/test_approval.py` | M2：状态机流转、审核日志、非法流转、审核权限 |
| `tests/test_enrollment.py` | M3：报名四项检查、并发不超卖、24 小时锁定、我的报名 |
| `tests/test_checkin.py` | M4：签到码、手动核验、撤销、重复签到 |
| `tests/test_stats.py` | M5：签到率 / 上座率口径、CSV 的 BOM 与转义、汇总口径 |
| `tests/test_admin_console.py` | F8：分类增删改与引用校验、用户与角色、两道「别把自己锁在门外」的护栏 |
| `tests/test_seed.py` | M6：`flask seed-demo` 的联调冒烟 |
| `tests/test_server_smoke.py` | M6：**真实起服务器**的端到端冒烟（默认跳过，`pytest -m smoke` 运行） |
| `tests/test_auth.py` / `test_permissions.py` / `test_security.py` / `test_password_reset.py` | M0 的安全基线（原有） |
| `tests/test_demo.py` + `tests/demo_harness.cjs` | 静态原型 `demo/index.html` 的走查（原有） |

### 10.2 为什么要有 `test_server_smoke.py`

其余测试都走 Flask 的测试客户端，它把 WSGI 调用**短路**了——`run.py`、
`DATABASE_URL` 环境变量、`flask` 命令行这几段接线，测试客户端一个都覆盖不到。
「测试全绿但 `python run.py` 起不来」是真会发生的。

所以单独留一个文件真起服务器、用 HTTP 走主链路。它要占端口、跑十来秒，
因此用 `smoke` 标记 **默认跳过**：

```bat
.venv\Scripts\python.exe -m pytest            :: 日常快速反馈
.venv\Scripts\python.exe -m pytest -m smoke   :: 交付前跑一次
```

### 10.3 两条容易写错的测试约定

1. **并发测试必须用文件型 SQLite。** 内存库是「每个连接一份独立数据」，
   多线程各写各的，测出来的「没有超卖」是假象。见 `concurrent_app` 装置。
2. **时间边界要直接调规则层。** 走 HTTP 光是请求开销就有几十毫秒，
   「截止前后各一秒」这种断言根本立不住；在同一个应用上下文里先设截止时间、
   立刻调用 `services.enroll()` 才卡得住秒级边界。

## 11. 待确认事项

1. 是否需要"忘记密码 / 邮箱验证"功能？（当前只能由管理员重置密码）
2. 活动取消后，是否需要通知已报名学生？（当前列为 V1.1）
3. 是否允许未报名的学生现场签到？（当前不支持，需拆表）
4. 前端静态资源是否改为自托管？当前从 CDN 加载 Bootstrap 且未加完整性校验。
5. 名单锁定的 24 小时窗口是否合适？是否需要按活动类型区分？

> 已确认：技术栈采用 **Python + Flask**；社团账户由管理员统一创建，不开放自助注册。
> 完整的待确认清单见 [`product-design.md`](./product-design.md) 第 11 节。

## 12. 本地运行

### 12.1 首次准备

```bat
:: 1. 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2. 建表
.venv\Scripts\python.exe -m flask --app run:app init-db

:: 3. 写入演示数据（分类、三个角色的账号、覆盖全部七种状态的活动与报名记录）
.venv\Scripts\python.exe -m flask --app run:app seed-demo
```

> `seed-demo` 只用于初始化**空库**。库里已有用户时会报错退出，以免把真实数据盖掉；
> 确实要重播就加 `--reset`。

### 12.1.1 演示数据有「保质期」，过期了就重播一次

演示数据里所有时间都是**相对播种那一刻**算出来的：报名截止 +2 天、进行中的活动
+6 小时……所以它只新鲜一阵子。放久了会看到整页都是「已结束」「报名已截止」——
看着像功能坏了，其实是数据过期了。

一条命令重播（会清空现有数据）：

```bat
.venv\Scripts\python.exe -m flask --app run:app seed-demo --reset
```

最要紧的是那场「进行中」的活动（书法体验课）：演示数据里只有它一场是进行中，
「凭签到码签到」整条路径都靠它；它一过期，签到页就只剩一句「活动已结束」。

### 12.2 启动

**最省事的方式：双击项目根目录下的 `启动系统.bat`。**
它会启动服务、自动打开浏览器，然后自己关掉。关掉那个黑窗口不影响服务运行。
要停的时候双击 `停止系统.bat`。

想自己控制的话，用命令行：

```bat
.venv\Scripts\python.exe run.py
```

打开 <http://127.0.0.1:5000>。这条命令带自动重载，改完代码存盘就生效，**写代码时用这条**。

只想打开页面看看、不想占着一个终端窗口的话，用后台启动器：

```bat
.venv\Scripts\python.exe tools\serve.py start    :: 挂到后台，关掉终端也照跑
.venv\Scripts\python.exe tools\serve.py status   :: 看还在不在
.venv\Scripts\python.exe tools\serve.py stop     :: 停掉
```

它和 `run.py` 的差别：关掉 DEBUG（不留一个带交互式调试控制台的进程在后台）、
关掉自动重载（单进程，按 PID 停一个就够）、日志与 PID 文件写在系统临时目录
（不会被重载器监视，也不污染仓库）。

> `启动系统.bat` / `停止系统.bat` 必须保持 **CRLF 行尾 + 纯 ASCII**，两条都是踩出来的：
> LF 行尾会让 cmd.exe 把各行粘在一起解析、整个脚本崩掉；而 `chcp 65001` 下批处理里的
> 多字节字符会错位，**而且不是每行都出问题**（同文件里别的中文行正常，偏偏某一行被当成
> 命令执行）。所以中文提示一律由 `tools/serve.py` 输出，批处理只留 ASCII 调度。
> `tests/test_serve_tool.py` 里有对应的回归测试钉住这两条。

演示数据里所有账号的密码都是 `Campus-Demo-2026`：

| 账号 | 姓名 | 角色 | 用途 |
| --- | --- | --- | --- |
| `20230101` | 李明 | 学生 | 浏览、报名、签到 |
| `club001` | 小张 | 社团（摄影协会） | 发布活动、提交审核、名单与签到、统计 |
| `admin` | 陈老师 | 校级管理员 | 审核、全部活动、分类、用户、密码找回 |

### 12.3 建议的走查顺序

`seed-demo` 铺的数据刻意覆盖了平时很难自然出现的分支，按下面顺序走一遍，
等于把全部状态流转和全部拦截提示都确认了一次：

1. 以 `20230101` 进入「活动广场」，报「手机摄影技巧分享会」的名，看剩余名额 +1；
2. 再打开「暗房冲洗体验（小班）」（5 / 5 已满）和「校运会摄影志愿服务」（报名已截止），
   确认按钮置灰且**写出了具体原因**；
3. 以 `club001` 进入「我的活动」，能看到草稿 / 待审核 / 已驳回三种状态；
   在「发布活动」里新建一个并提交审核；
4. 以 `admin` 进入「审核中心」，通过一个、驳回一个（填原因）；
5. 切回 `club001`，被驳回的活动能看到原因并重新提交；
6. 在「报名与签到」里：报名人数 8、实到 5、签到率 62.5%，
   勾选一个人的签到，看签到率立刻变；导出 CSV，用 Excel 打开确认中文不乱码；
7. 以 `20230101` 打开「书法体验课」（进行中），输入签到码 `482913` 自助签到。

### 12.4 换数据库 / 清空重来

数据库默认落在 `instance/campus.db`。想从零开始，最省事的是：

```bat
.venv\Scripts\python.exe -m flask --app run:app seed-demo --reset
```

要连数据库文件一起换掉，用 `DATABASE_URL` 环境变量指定别的库：

```bat
set DATABASE_URL=sqlite:///D:/data/campus.db
```

### 12.5 生产部署注意

生产环境**不要**用 `run.py`。应该交给 WSGI 服务器，并且先设好环境变量：

```bat
:: 生成密钥
python -c "import secrets; print(secrets.token_hex(32))"

set SECRET_KEY=<上一步的输出>
set DATABASE_URL=<你的数据库连接串>
set FLASK_CONFIG=prod
```

缺 `SECRET_KEY`、密钥过短或用了开发占位值时，应用会**直接拒绝启动**——
带病启动比启动失败危险得多。

## 13. 发布到线上

`deploy.py` 是这个项目**唯一**为「挂到公网上跑」准备的入口。它和 `run.py`
的差别都是环境逼出来的：

| 要求 | 为什么 |
| --- | --- |
| 绑定 `0.0.0.0`、端口读环境变量 `PORT` | 发布沙箱只给一个 HTTP 端口，并注入 `PORT`。只监听回环地址的话反向代理连不上 |
| `FLASK_CONFIG=prod`（DEBUG 关闭） | 开发配置会打开 Werkzeug 的交互式调试控制台，**放在公网上等于把任意代码执行能力开放给所有人** |
| 启动时自动建表 + 空库播种 | 沙箱里没有运维去跑 `init-db` / `seed-demo` |
| 数据库落在 `instance/live.db` | 和本地开发的 `campus.db` 分开：线上不会被本地那份旧数据污染，本地调试也不会误改线上 |

```bat
python deploy.py                          :: 本地模拟
PORT=8080 python deploy.py                :: 线上由沙箱注入 PORT
SESSION_COOKIE_SECURE=0 python deploy.py  :: 站点确认是按 HTTP 访问时才加
```

**关于 `SESSION_COOKIE_SECURE`**：生产配置默认把 session Cookie 标成 Secure
（只允许 HTTPS 传输）。如果站点实际是按 HTTP 访问的，浏览器和任何客户端都
**不会回传**这个 Cookie，session 直接丢——表现出来是「一提交登录就 400
BAD REQUEST」（CSRF token 对不上）。这个现象和「密码错了」「CSRF 配置错了」
长得几乎一样，很容易查偏方向，所以留了个显式开关。

**三条必须知道的限制**：

1. **演示账号的密码是公开的**（`Campus-Demo-2026`），而线上是公开地址——
   任何人都能用 `admin` 登录并改动数据。所以站点里**只能放演示数据**，
   绝不能放真实的学生信息。
2. **用的是 Flask 自带的开发服务器**。课程演示够用，但它不是为公网设计的；
   真要长期对外服务，应换成 waitress / gunicorn。
3. **SECRET_KEY 每次启动随机生成**，意味着重启后所有人的登录会话一起失效。
   真实部署必须从环境变量注入一个固定密钥。

