# 05 - PyPI 发布

## 概览

本文件只说明 Somnia 的 Python 包发布到 PyPI。

- 正式环境：`https://pypi.org/project/somnia/`
- 测试环境：`https://test.pypi.org/`

桌面端发布已独立到单独文档：

- `Docs/运维/10-桌面端发布.md`

## 当前发布方式

Somnia 的 PyPI 发布由 GitHub Actions 自动执行，入口 workflow 为：

- `.github/workflows/publish.yml`

说明：

- PyPI 发布现在和桌面端发布共用同一套 tag 发布流水线。
- 本文只关注其中的 `publish-pypi` 部分。

## 触发方式

正式发布通过推送版本 tag 触发：

```powershell
powershell -File scripts\release.ps1 0.4.0
```

这会创建并推送类似 `v0.4.0` 的 tag，随后触发 `publish.yml`。

## 发布前检查

发布前至少确认：

- `VERSION` 文件已更新
- 版本号未在 PyPI 发布过
- GitHub Secrets 中已配置 `PYPI_API_TOKEN`

## CI 自动发布

`publish.yml` 中与 PyPI 相关的流程是：

1. `verify-version`
   校验 `VERSION` 与 tag 一致。
2. `publish-pypi`
   执行构建、校验和上传。

实际执行的核心命令等价于：

```powershell
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

## 手动发布

如果需要绕过 CI，可手动发布：

```powershell
pip install build twine
Remove-Item dist -Recurse -Force -ErrorAction SilentlyContinue
python -m build
python -m twine check dist/*
python -m twine upload dist/* -u __token__ -p "pypi-你的Token"
```

如果只想发到 TestPyPI：

```powershell
python -m twine upload --repository testpypi dist/* -u __token__ -p "你的TestPyPI-Token"
```

## 包配置

`pyproject.toml` 关键配置：

```toml
[project]
name = "somnia"
dynamic = ["version"]
requires-python = ">=3.11"

[project.scripts]
somnia = "open_somnia.cli.main:main"

[tool.setuptools.dynamic]
version = { file = "VERSION" }
```

## 发布后验证

```powershell
pip index versions somnia
pip install --upgrade somnia
somnia --help
pip show somnia
```

## Secrets

当前 PyPI 发布依赖：

- `PYPI_API_TOKEN`

## 常见问题

### 1. VERSION 与 tag 不一致

症状：

```text
VERSION file (...) does not match tag (...)
```

处理：

- 修正 `VERSION`
- 重新打 tag

### 2. PyPI 版本已存在

症状：

```text
HTTPError: 400 Bad Request from https://upload.pypi.org/legacy/
File already exists.
```

处理：

- 增加版本号
- 重新发布

### 3. Token 权限不足

症状：

```text
HTTPError: 403 Forbidden from https://upload.pypi.org/legacy/
```

处理：

- 确认 `PYPI_API_TOKEN` 有对应项目权限
- 确认上传的包名确实是 `somnia`
