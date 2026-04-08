# 05 - PyPI 发布

## 概述

PyPI（Python Package Index）是 Python 官方包仓库。用户通过 `pip install somnia` 安装。

- **正式环境**：https://pypi.org/project/somnia/
- **测试环境**：https://test.pypi.org/

## 账号信息

| 项目 | 值 |
|------|-----|
| 用户名 | dongkeqi |
| 2FA | 已启用（TOTP） |
| API Token | 已创建（存于 GitHub Secrets: `PYPI_API_TOKEN`） |

## 发布方式

### 方式一：通过 CI 自动发布（推荐）

推送 tag 后 GitHub Actions 自动执行：

```powershell
powershell -File scripts\release.ps1 0.4.0
# CI 自动 twine upload
```

### 方式二：手动发布

```powershell
cd OpenAgent

# 1. 构建
pip install build twine
Remove-Item dist -Recurse -Force -ErrorAction SilentlyContinue
python -m build

# 2. 检查
twine check dist/*

# 3. 上传
twine upload dist/* -u __token__ -p "pypi-你的Token"

# 上传到 TestPyPI 测试
twine upload --repository testpypi dist/* -u __token__ -p "你的TestPyPI-Token"
```

## 包配置

`pyproject.toml` 关键配置：

```toml
[project]
name = "somnia"                # PyPI 包名
dynamic = ["version"]          # 从 VERSION 文件读取
requires-python = ">=3.11"

[project.scripts]
somnia = "openagent.cli.main:main"   # 安装后注册 somnia 命令

[tool.setuptools.dynamic]
version = {file = "VERSION"}         # 版本号来源
```

## 发布后验证

```powershell
# 等 1-2 分钟 PyPI 缓存刷新后

# 查看版本
pip index versions somnia

# 安装测试
pip install --upgrade somnia

# 验证命令
somnia --help

# 查看包信息
pip show somnia
```

## 常见问题

### 1. 版本号已存在

```
HTTPError: 400 Bad Request from https://upload.pypi.org/legacy/
File already exists.
```

**原因**：该版本号已发布过，PyPI 不允许覆盖。

**解决**：递增版本号，重新发布。

### 2. 包名冲突

```
HTTPError: 403 Forbidden from https://upload.pypi.org/legacy
The user 'dongkeqi' isn't allowed to upload to 'somnia'.
```

**原因**：包名被他人占用，或 Token 权限不足。

**解决**：
- 确认包名在 PyPI 上未被占用
- 确认 Token 的 scope 包含 `somnia` 项目

### 3. 2FA 验证

发布时如果使用账号密码而非 API Token，需要 2FA 验证码。

```powershell
# 生成 2FA 验证码（需要 pyotp）
python scripts\get2fa.py
```

> ⚠️ `get2fa.py` 包含敏感信息，已加入 `.gitignore`，不会提交到 git。

### 4. 清理残留的 .dist-info

```powershell
# 查看残留
Get-ChildItem "D:\APP\Python\Lib\site-packages" -Directory -Filter "~*"

# 清理
Get-ChildItem "D:\APP\Python\Lib\site-packages" -Directory -Filter "~*" | Remove-Item -Recurse -Force
```
