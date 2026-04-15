# 04 - CI/CD 配置

## 文件位置

`.github/workflows/publish.yml`

## 触发条件

| 触发方式 | 说明 |
|---------|------|
| `push tags: v*` | 推送 tag（如 v0.4.0）时自动触发 |
| `workflow_dispatch` | GitHub Actions 页面手动触发 |

## CI 流程图

```
git push tag v0.4.0
        │
        ▼
  ┌─────────────────┐
  │ verify-version   │  检查 VERSION 文件与 tag 版本号一致
  └────────┬────────┘
           │
     ┌─────┴──────┐
     ▼            ▼
┌──────────┐ ┌────────────┐
│publish-  │ │publish-npm │  npm publish
│  pypi    │ │            │  (continue-on-error)
│twine     │ │            │  没配 NPM_TOKEN 时不阻塞
│upload    │ │            │
└────┬─────┘ └─────┬──────┘
     │             │
     └──────┬──────┘
            ▼
   ┌─────────────────┐
   │ create-release   │  自动创建 GitHub Release
   │ (读取 CHANGELOG) │  含安装说明和 PyPI 链接
   └─────────────────┘
```

## GitHub Secrets 配置

在 GitHub 仓库 → **Settings → Secrets and variables → Actions** 中配置：

| Secret 名 | 必须 | 说明 | 获取方式 |
|-----------|------|------|---------|
| `PYPI_API_TOKEN` | ✅ 是 | PyPI 发布凭证 | https://pypi.org → Account settings → API tokens |
| `NPM_TOKEN` | ❌ 否 | npm 发布凭证 | https://www.npmjs.com → Access Tokens |

### 创建 PyPI API Token

1. 登录 https://pypi.org
2. Account settings → API tokens → Add API token
3. Token name: `somnia-publish`
4. Scope: `somnia` 项目（或 "Entire account"）
5. 复制 token（以 `pypi-` 开头）
6. 粘贴到 GitHub Secrets → `PYPI_API_TOKEN`

### 创建 npm Token（可选）

1. 登录 https://www.npmjs.com
2. Access Tokens → Generate New Token → Classic Token
3. Type: Publish
4. 复制 token
5. 粘贴到 GitHub Secrets → `NPM_TOKEN`

## CI Job 详情

### verify-version

- 检查 `VERSION` 文件内容与 git tag 版本号一致
- 不一致则中止发布

### publish-pypi

- Python 3.12 环境
- `pip install build twine`
- `python -m build` 构建包
- `twine check dist/*` 检查包
- `twine upload dist/*` 上传到 PyPI

### publish-npm

- Node.js 20 环境
- `npm publish --access public`
- `continue-on-error: true`（没配 Token 时不阻塞后续）

### create-release

- 从 `CHANGELOG.md` 读取当前版本的变更记录
- 使用 `softprops/action-gh-release@v2` 创建 GitHub Release
- 自动判断是否预发布版（含 rc/beta/alpha 的为预发布）

## 注意事项

1. **tag 必须推送到 GitHub 才能触发 CI**，本地打 tag 不够
2. **分支限制**：当前在 `OpenAgent_by_codex` 分支，确保该分支的 tag 推送能触发 Actions
3. **版本号不可重复**：PyPI 不允许重复上传同一版本号
4. **npm 包名唯一**：如果 npm 上已被占用，需要换名或联系占用人

## 排查 CI 失败

```bash
# 查看远程 tag 是否推送成功
git ls-remote --tags github.com

# 查看 CI 运行状态
# 浏览器打开: https://github.com/keiqkeqi321/learn-claude-code/actions
```

常见失败原因：

| 错误 | 原因 | 解决 |
|------|------|------|
| `403 Forbidden` | PYPI_API_TOKEN 无效或过期 | 重新生成 Token |
| `400 File already exists` | 版本号已发布过 | 递增版本号 |
| `Unrecognized named-value` | YAML 语法错误 | 检查 workflow 文件 |
| Tag 推送后 CI 未触发 | 分支/tag 配置问题 | 检查默认分支设置 |
