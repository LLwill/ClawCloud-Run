# Vaultwarden 全自动化配置指南

> 通过 Vaultwarden/Bitwarden 实现完全无人值守的多账号自动登录

## 📋 目录

1. [功能说明](#功能说明)
2. [前置条件](#前置条件)
3. [Vaultwarden 配置](#vaultwarden-配置)
4. [GitHub 配置](#github-配置)
5. [多账号设置](#多账号设置)
6. [测试验证](#测试验证)
7. [故障排查](#故障排查)

---

## 🎯 功能说明

### **传统方式（半自动）：**
```
GitHub 密码: 手动配置在 Secrets
2FA 验证码: 通过 Telegram 手动输入 /code 123456
```

### **Vaultwarden 方式（全自动）：**
```
GitHub 密码: 自动从 Vaultwarden 获取
2FA 验证码: 自动从 Vaultwarden 生成
无需任何人工干预！🎉
```

---

## ⚙️ 前置条件

### 1. Vaultwarden 实例
- 自建 Vaultwarden 服务器（推荐）
- 或使用 Bitwarden 官方服务（需要 Premium）

### 2. Bitwarden API Key
- 登录 Vaultwarden Web 界面
- 前往：设置 → 安全 → API 密钥
- 点击"查看 API 密钥"，输入主密码
- 复制 `client_id` 和 `client_secret`

### 3. GitHub 账号开启 2FA
- 必须启用 TOTP（Authenticator App）方式
- Mobile 验证目前不支持自动化

---

## 🔐 Vaultwarden 配置

### 步骤 1: 在 Vaultwarden 中创建 GitHub 条目

#### 单账号示例：
```
条目名称: GitHub（或任意名称，推荐 GitHub-acc1）
用户名: your-email@example.com
密码: your-github-password
TOTP: otpauth://totp/...（从 GitHub 2FA 设置获取）

Custom Fields:
  account_id (Text) = acc1  ← 核心！用于关联
```

#### 多账号示例：
```
账号 1:
  条目名称: GitHub-acc1（推荐约定，但不强制）
  用户名: user1@example.com
  密码: ***
  TOTP: otpauth://...
  Custom Fields:
    account_id = acc1  ← 必填！

账号 2:
  条目名称: GitHub-acc2
  用户名: user2@example.com
  密码: ***
  TOTP: otpauth://...
  Custom Fields:
    account_id = acc2

账号 3:
  条目名称: 我的工作GitHub  ← 名称随意
  用户名: work@company.com
  密码: ***
  TOTP: otpauth://...
  Custom Fields:
    account_id = acc3  ← 关键！只要这个对就行
```

### 步骤 2: 添加 Custom Field

在 Vaultwarden Web 界面：

1. 打开 GitHub 条目
2. 滚动到底部
3. 点击 "+ 新增自定义字段"
4. 类型：选择 **文本 (Text)**
5. 名称：`account_id`
6. 值：`acc1`（或 acc2, acc3...）
7. 保存

**重要：**
- Custom Field 名称必须是 `account_id`（区分大小写）
- 值必须与配置文件中的 ID 一致
- 条目名称可以随意，但推荐使用 `GitHub-{id}` 便于识别

### 步骤 3: 配置 TOTP

#### 方式 A: 从 GitHub 新设置 2FA

1. 浏览器登录 GitHub → 设置 → 密码和身份验证
2. 开启两步验证 → 选择 Authenticator App
3. **不要扫码！** 点击 "手动输入代码"
4. 复制显示的密钥（如 `abcd1234efgh5678`）
5. 在 Vaultwarden 条目中，TOTP 字段填入：
   ```
   otpauth://totp/GitHub:your-email@example.com?secret=abcd1234efgh5678&issuer=GitHub
   ```
6. 返回 GitHub，在 Vaultwarden 中查看生成的 6 位验证码，填入完成设置

#### 方式 B: 从现有 Authenticator App 迁移

1. 如果你已经在 Google Authenticator/Authy 中设置
2. 需要导出种子（不同 App 方式不同）
3. 或者在 GitHub 重新设置 2FA（推荐）

---

## 🔧 GitHub 配置

### 步骤 1: 创建 GitHub Secrets

前往你的仓库：`Settings → Secrets and variables → Actions`

#### 必需 Secrets（Vaultwarden）：

| Secret 名称 | 值示例 | 说明 |
|-------------|--------|------|
| `BW_SERVER_URL` | `https://vault.example.com` | Vaultwarden 服务器地址 |
| `BW_CLIENTID` | `user.xxx` | API Key Client ID |
| `BW_CLIENTSECRET` | `xxx...` | API Key Client Secret |
| `BW_PASSWORD` | `your-master-password` | Vaultwarden 主密码 |

#### 必需 Secrets（其他）：

| Secret 名称 | 说明 |
|-------------|------|
| `TG_BOT_TOKEN` | Telegram Bot Token |
| `TG_CHAT_ID` | Telegram Chat ID |
| `REPO_TOKEN` | GitHub PAT（用于更新 Cookie） |

#### Secrets（Cookie 存储）：

**单账号：**
- `GH_SESSION`（首次可不设置，会自动创建）

**多账号：**
- `GH_SESSION_ACC1`
- `GH_SESSION_ACC2`
- `GH_SESSION_ACC3`
- ... 按需添加

**注意：** Cookie Secret 首次可以留空，运行后会自动创建。

### 步骤 2: 创建 accounts.yml 配置

创建文件：`.github/config/accounts.yml`

```yaml
accounts:
  - id: acc1
    display_name: "个人账号"
    enabled: true

  - id: acc2
    display_name: "工作账号"
    enabled: true

  - id: acc3
    display_name: "开源账号"
    enabled: true
```

**说明：**
- `id`: 必须与 Vaultwarden Custom Field `account_id` 匹配
- `display_name`: 用于日志和通知显示
- `enabled`: true=启用，false=禁用

---

## 🚀 多账号设置

### 完整配置示例（3 个账号）

#### 1. Vaultwarden 配置

```
条目 1:
  名称: GitHub-acc1
  用户名: user1@example.com
  密码: ***
  TOTP: otpauth://totp/...
  Custom Field: account_id = acc1

条目 2:
  名称: GitHub-acc2
  用户名: work@company.com
  密码: ***
  TOTP: otpauth://totp/...
  Custom Field: account_id = acc2

条目 3:
  名称: GitHub-acc3
  用户名: oss@example.com
  密码: ***
  TOTP: otpauth://totp/...
  Custom Field: account_id = acc3
```

#### 2. GitHub Secrets

```
BW_SERVER_URL = https://vault.example.com
BW_CLIENTID = user.xxx
BW_CLIENTSECRET = xxx
BW_PASSWORD = master-password

GH_SESSION_ACC1 = (自动生成)
GH_SESSION_ACC2 = (自动生成)
GH_SESSION_ACC3 = (自动生成)

TG_BOT_TOKEN = 123:ABC...
TG_CHAT_ID = 123456
REPO_TOKEN = ghp_xxx
```

#### 3. accounts.yml

```yaml
accounts:
  - id: acc1
    display_name: "个人账号"
    enabled: true
  - id: acc2
    display_name: "工作账号"
    enabled: true
  - id: acc3
    display_name: "开源账号"
    enabled: true
```

### 运行流程

```
┌─────────────────────────────────────────────────────────┐
│ GitHub Actions 启动                                      │
├─────────────────────────────────────────────────────────┤
│ 1. 读取 accounts.yml → 生成 Matrix                      │
│ 2. 串行运行（避免冲突）：                                │
│    ├─ 账号 acc1 (个人账号)                              │
│    │  ├─ 连接 Vaultwarden                               │
│    │  ├─ 通过 Custom Field 查找条目                     │
│    │  ├─ 获取用户名/密码/TOTP                            │
│    │  ├─ 自动登录 GitHub                                │
│    │  ├─ 自动填入 TOTP                                  │
│    │  └─ 保存 Cookie 到 GH_SESSION_ACC1                │
│    │                                                     │
│    ├─ 间隔 30 秒                                        │
│    │                                                     │
│    ├─ 账号 acc2 (工作账号)                              │
│    │  └─ ... 同上                                       │
│    │                                                     │
│    └─ 账号 acc3 (开源账号)                              │
│       └─ ... 同上                                       │
│                                                          │
│ 3. Telegram 通知：每个账号的运行结果                     │
└─────────────────────────────────────────────────────────┘
```

---

## ✅ 测试验证

### 步骤 1: 手动触发测试

1. 前往仓库 → Actions
2. 选择 "ClawCloud 多账号自动登录"
3. 点击 "Run workflow"
4. （可选）指定账号 ID，如 `acc1` 只测试单个账号
5. 点击绿色 "Run workflow" 按钮

### 步骤 2: 查看日志

```
✅ 账号: 个人账号 (ID: acc1)
✅ Session Secret: GH_SESSION_ACC1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Vaultwarden 连接成功
✅ 通过 Custom Field 找到条目: GitHub-acc1
✅ 已从 Vaultwarden 获取凭据
   条目: GitHub-acc1
   用户: user1@example.com
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
...
✅ 已从 Vaultwarden 自动获取 TOTP
✅ 验证码验证通过
✅ 已更新 GH_SESSION_ACC1
```

### 步骤 3: 检查 Telegram 通知

```
🤖 ClawCloud 自动登录

状态: ✅ 成功
账号: 个人账号
用户: user1@example.com
时间: 2026-03-04 09:30:00

---

🔑 Cookie 已自动更新

账号: 个人账号
用户: user1@example.com
Secret: GH_SESSION_ACC1
```

---

## 🔧 故障排查

### 问题 1: 找不到 Vaultwarden 条目

**错误：**
```
⚠️ 未找到账号 acc1 的凭据
```

**解决：**
1. 检查 Vaultwarden 中是否存在 Custom Field `account_id = acc1`
2. 检查字段名是否准确（区分大小写）
3. 检查 BW_CLIENTID/SECRET/PASSWORD 是否正确

### 问题 2: Vaultwarden 连接失败

**错误：**
```
[VaultwardenClient] 登录失败
```

**解决：**
1. 检查 `BW_SERVER_URL` 是否正确（包含 https://）
2. 检查 API Key 是否有效
3. 检查主密码是否正确
4. 尝试在本地运行 `bw login --apikey` 测试

### 问题 3: TOTP 验证失败

**错误：**
```
验证码可能错误
```

**解决：**
1. 检查 Vaultwarden 中 TOTP 格式：`otpauth://totp/...`
2. 确认 GitHub 2FA 使用的是 Authenticator App（不是 SMS）
3. 时间同步问题：确保服务器时间准确

### 问题 4: Cookie 保存失败

**错误：**
```
更新 Secret 失败
```

**解决：**
1. 检查 `REPO_TOKEN` 是否有 `repo` 权限
2. 检查 Secret 名称是否正确（GH_SESSION_ACC1）
3. 手动创建对应的 Secret（值可为空）

---

## 📚 相关链接

- [Bitwarden CLI 文档](https://bitwarden.com/help/cli/)
- [Vaultwarden GitHub](https://github.com/dani-garcia/vaultwarden)
- [TOTP RFC 6238](https://datatracker.ietf.org/doc/html/rfc6238)

---

## 💡 最佳实践

1. **定期备份** Vaultwarden 数据
2. **使用强主密码** 保护 Vaultwarden
3. **定期更新** Bitwarden CLI 到最新版本
4. **测试降级** 确保 Telegram 手动输入仍然可用
5. **监控通知** 及时发现登录失败

---

配置完成后，你的多个 GitHub 账号将实现完全无人值守的自动登录！🎉
