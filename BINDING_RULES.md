# 会话-证书绑定校验：规则说明与交付数据

语言：Python 3（仅标准库：`hashlib` / `hmac` / `time` / `dataclasses` / `enum` / `unittest` / `tracemalloc`）

## 1. 绑定标识的选取

```
binding_id = SHA-256( "SESSBIND/v1\n" || subject_dn || "\n" || spki_sha256_hex )
```

- `subject_dn`：证书主体 DN —— 覆盖**证书主体**。
- `spki_sha256_hex`：证书 SubjectPublicKeyInfo(DER) 的 SHA-256 —— 覆盖**公钥指纹**。
- 会话编号 `session_id` 只作查找键，**不参与任何认证决策**；因此绝不是"只绑会话编号"。
- 换证书必然换公钥（或主体），`binding_id` 必变；主体相同公钥不同、公钥相同主体不同，均判不匹配（有对应用例）。
- 比较用 `hmac.compare_digest` 做常量时间比较，防时序侧信道。

## 2. 判定规则（确定性顺序）

每次（重）握手按固定顺序判定，先证书有效性、后绑定一致性：

1. **过期**：`not_after <= now` → `REJECT_CERT_EXPIRED`
2. **吊销**：`serial ∈ 吊销列表` → `REJECT_CERT_REVOKED`
3. **绑定**：无旧会话 → `ACCEPT_NEW`（建立绑定）；一致 → `ACCEPT_RESUME`；不一致 → `REJECT_CERT_CHANGED`

所有针对已存在会话的拒绝判定都会**作废旧会话**（fail-closed）；任何不匹配都拒绝并给出原因，**绝不降级为单向认证放行**（`test_rejections_never_downgrade` 强制断言所有 REJECT 的 `accepted == False`）。

## 3. 结果对照表（与逐条手工核对一致）

| 用例 | 最终判定 | 旧会话 | 说明 |
|---|---|---|---|
| 首次连接（有效证书） | `ACCEPT_NEW` | —（新建） | 建立绑定 |
| 正常重连（同一证书） | `ACCEPT_RESUME` | 保留 | 绑定一致 |
| 换证书重连（换公钥） | `REJECT_CERT_CHANGED` | **作废** | 需重新完整认证 |
| 换证书重连（换主体） | `REJECT_CERT_CHANGED` | **作废** | 公钥相同也拒绝 |
| 证书被吊销后重连 | `REJECT_CERT_REVOKED` | **作废** | 吊销优先于绑定检查 |
| 证书过期后重连 | `REJECT_CERT_EXPIRED` | **作废** | 过期最先检查 |
| 首次连接即吊销/过期 | `REJECT_CERT_REVOKED` / `REJECT_CERT_EXPIRED` | 不建立 | 无效证书不得建会话 |

该表由 `CrossCheckTableTest` 程序化对拍：测试逐条执行操作序列并与手工预期断言一致，同时打印上表供人工复核。

## 4. 对拍数据（黄金向量）

黄金值用**与库无关的** coreutils `sha256sum` 手工计算，测试逐条比对：

```
spki_fp_A = printf '\x01\x02\x03\x04' | sha256sum
          = 9f64a747e1b97f131fabb6b447296c9b6f0201e79fb3c5356e6c77e89b6a806a
binding_A = printf 'SESSBIND/v1\nCN=alice,O=Example\n<spki_fp_A>' | sha256sum
          = e2f5edc26d4d59e4a7e9e03cbb9d0ebc9d2730ea4f6d70638fb13fede14c32d8
binding_B(换公钥) = 740efc5ec96b309255d9a4375d9652ad82d1bd88fcf624feffcb470d28a97ff8
binding_C(换主体) = aa86dc95bdbadabf74a2f1c21f5e0dcd5a2d5a8d39bca3866a5817fc9dac9c1c
```

`GoldenVectorTest` 断言库输出与以上手工值逐条相等 → 绑定结果与手工核对一致。

## 5. 10 万会话基准（实测，CPython 3.12，Linux x86-64）

| 阶段 | 耗时 | 吞吐 |
|---|---|---|
| 100,000 次首次连接（建立绑定） | 1.197 s | ≈ 83,600 ops/s |
| 100,000 次正常重连校验 | 0.736 s | ≈ 135,900 ops/s |
| 100,000 次换证书拒绝+作废 | 1.218 s | ≈ 82,100 ops/s |
| 三阶段合计 | 3.150 s | — |

- **内存上界**：O(n)，每会话 1 条记录（32B 绑定标识 + 主体 DN + 公钥指纹 + 过期时间）。
  满载 100,000 会话实测峰值 **37.7 MiB，约 395 字节/会话**（tracemalloc）。
- 单次校验耗时 O(1)：一次 SHA-256（公钥指纹）+ 一次 SHA-256（绑定标识）+ 一次 dict 查找。

## 6. 边界用例（均已覆盖）

- `not_after == now` 恰到期 → 按过期拒绝（`test_not_after_equals_now_is_expired`）。
- 只换公钥不换主体 / 只换主体不换公钥 → 均判绑定不匹配。
- 首次连接即出示吊销/过期证书 → 拒绝且不建立会话。
- 旧会话作废后，同一 `session_id` 重新完整认证 → `ACCEPT_NEW` 建立新绑定。
- 多会话互不影响：一会话被拒作废不波及其他会话。
- 判定顺序确定：过期 > 吊销 > 绑定，同一输入永远同一结果。

## 7. 运行命令

```bash
python3 -m unittest -v test_cert_session_binding   # 自测（15 个用例 + 对照表对拍）
python3 bench_binding.py                            # 10 万会话耗时/内存基准
python3 bench_binding.py 1000000                    # 自定义规模
```

## 8. 文件清单

- `cert_session_binding.py` —— 库源码（`SessionBinder.validate()` 为主入口）
- `test_cert_session_binding.py` —— 自测（黄金向量对拍 + 流程用例 + 对照表对拍）
- `bench_binding.py` —— 10 万会话耗时与内存基准
- `BINDING_RULES.md` —— 本文档
