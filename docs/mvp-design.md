# 校园活动管理系统 — MVP 设计方案

> 版本：v0.1　最后更新：2026-09-17
> 本文档为 MVP 阶段的范围基准与实现依据，范围变更须同步修改本文档。

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
| 技术栈 | 无外部约束，由本文档推荐 |

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

1. **登录与角色权限** — 学号 / 工号 + 密码登录，JWT 会话，三角色权限拦截。
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

统一前缀 `/api`，鉴权用 `Authorization: Bearer <token>`。

### 认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/login` | 登录，返回 token 与用户信息 |
| POST | `/auth/logout` | 登出 |
| GET | `/auth/me` | 当前登录用户 |

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
| GET | `/admin/activities/pending` | ADMIN | 待审核列表 |
| GET / PUT | `/admin/users/{id}` | ADMIN | 用户查询与角色调整 |

## 8. 技术栈建议

无外部约束，按"最短路径 + 资料充足"推荐：

**推荐（Java 路线）**

- 前端：Vue 3 + Vite + Element Plus + Pinia + Vue Router + Axios
- 后端：Spring Boot 3 + MyBatis-Plus + Spring Security（JWT）+ MySQL 8
- 测试：JUnit 5 + MockMvc，并发报名用 CountDownLatch 模拟

**备选（Node 路线，依赖更少，启动更快）**

- 前端同上
- 后端：NestJS + Prisma + MySQL 8
- 测试：Jest + Supertest

> 两条路线能力等价。若课程或比赛有指定语言，以指定为准。

## 9. 开发里程碑

每个里程碑至少对应一个 commit，提交信息遵循 `type(scope): 描述`。

| 里程碑 | 内容 | 交付判据 |
| --- | --- | --- |
| M0 | 前后端脚手架、数据库连接、登录鉴权打通 | 能用三种角色登录取到 token |
| M1 | 分类与活动 CRUD、活动列表 / 详情 | 管理员能建活动并在列表看到 |
| M2 | 状态机 + 审核流 + 角色权限拦截 | 社团提交 → 管理员审核 → 学生可见 |
| M3 | 报名与取消（含并发控制） | 并发压测下报名数不超过 capacity |
| M4 | 签到（签到码 + 手动核验） | 学生凭码签到成功，重复签到被拒 |
| M5 | 统计与 CSV 导出 | 签到率计算正确，导出文件可打开 |
| M6 | 测试补齐、异常分支、联调 | 全部测试通过，覆盖第 6 节三个风险点 |

## 10. 测试要求

依据 `AGENTS.md`，每次改动后必须编写或更新测试，并在交付前确保全部通过。重点覆盖：

- **并发报名**：多线程同时报名，断言 `enrolled_count <= capacity` 且无重复记录
- **权限越权**：学生调用创建活动接口、社团编辑他人活动，均应返回 403
- **状态流转**：非法流转（如对 `FINISHED` 活动报名）必须被拒绝
- **时间边界**：报名截止前后各一秒的行为
- **重复操作**：重复报名、重复签到、重复取消

## 11. 待确认事项

1. 技术栈选 Java 路线还是 Node 路线？
2. 是否需要"忘记密码 / 邮箱验证"功能？
3. 活动取消后，是否需要通知已报名学生？（当前列为 V1.1）
4. 是否允许未报名的学生现场签到？（当前不支持，需拆表）
5. 社团账户由管理员统一创建，还是开放注册后申请升级？（当前按管理员创建）
