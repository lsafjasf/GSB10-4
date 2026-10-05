# 冲突报告

## 报告语义

同一请求在同一优先级命中两条或更多规则时，匹配器不选择任何一条，而是抛出 `RouteConflictError`。报告列出每个冲突优先级的全部命中规则，规则按 `id` 字典序排列，和加载顺序无关。

默认匹配会检查所有优先级，因此也会报告被高优先级规则遮蔽的低优先级冲突。`audit=True` 用于配置审计；在没有冲突时，它会在成功结果中返回所有优先级的命中项。

## 示例一：最高命中优先级冲突

规则：

| id | pattern | priority |
| --- | --- | ---: |
| `r-api` | `/api/*` | 10 |
| `r-users` | `/api/users/*` | 10 |
| `r-exact` | `/api/users/42` | 10 |
| `r-middle` | `/api/*/42` | 10 |
| `r-lower` | `/api/users/42` | 1 |

请求：`/api/users/42`

实际输出：

```text
route conflict for '/api/users/42': priority 10: 4 matches (r-api, r-exact, r-middle, r-users)
  priority=10 id=r-api pattern='/api/*'
  priority=10 id=r-exact pattern='/api/users/42'
  priority=10 id=r-middle pattern='/api/*/42'
  priority=10 id=r-users pattern='/api/users/*'
```

`r-lower` 被优先级 10 遮蔽，且它在优先级 1 是唯一命中，因此不是冲突项，不会出现在冲突列表中。

## 示例二：被高优先级遮蔽的低优先级冲突

规则：

| id | pattern | priority |
| --- | --- | ---: |
| `r-high` | `/api/*` | 100 |
| `r-low-a` | `/api/users/*` | 1 |
| `r-low-b` | `/api/*/42` | 1 |

请求：`/api/users/42`

默认模式输出（高优先级唯一命中不会掩盖低优先级冲突）：

```text
route conflict for '/api/users/42': priority 1: 2 matches (r-low-a, r-low-b)
  priority=1 id=r-low-a pattern='/api/users/*'
  priority=1 id=r-low-b pattern='/api/*/42'
```

审计模式对冲突使用同一份报告；如果没有任何冲突，审计模式的成功结果会额外携带所有优先级的命中项。

## 复现

命令：

```bash
python3 examples/conflict_demo.py
```
