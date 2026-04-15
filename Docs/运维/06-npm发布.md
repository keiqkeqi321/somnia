# 06 - npm 发布

## 概述

npm 包是一个 **wrapper**，本身不包含逻辑，功能：
1. 检测本地 Python 环境
2. 自动 `pip install somnia`
3. 调用 `python -m open_somnia` 运行

用户通过 `npx somnia` 或 `npm install -g somnia` 使用。

## 当前状态

| 项目 | 状态 |
|------|------|
| npm 包名 | `somnia` |
| 包地址 | https://www.npmjs.com/package/somnia |
| 发布状态 | ⚠️ **未发布**（未配置 NPM_TOKEN） |
| CI 集成 | 已配置但 `continue-on-error: true` |

## 发布前置条件

1. npm 账号已注册：https://www.npmjs.com/signup
2. 本地已登录：`npm login`
3. GitHub Secrets 中配置 `NPM_TOKEN`

## 创建 npm Token

```bash
# 方式一：命令行
npm token create

# 方式二：网页
# https://www.npmjs.com → Access Tokens → Generate New Token
# Type: Publish
```

拿到 Token 后配置到 GitHub Secrets：

```
GitHub → Settings → Secrets → Actions → NPM_TOKEN = <你的Token>
```

## 手动发布

```powershell
cd OpenAgent\npm

# 预览（查看将要发布的文件）
npm pack --dry-run

# 发布
npm publish --access public
```

## npm 包结构

```
npm/
├── package.json        # 包配置
├── bin/
│   └── somnia.js         # CLI 入口（注册为 somnia 命令）
├── postinstall.js      # npm install 后自动 pip install somnia
├── index.js            # 模块入口
└── LICENSE             # MIT
```

### package.json 关键配置

```json
{
  "name": "somnia",
  "bin": {
    "somnia": "./bin/somnia.js"
  },
  "scripts": {
    "postinstall": "node postinstall.js"
  }
}
```

### CLI 入口行为（bin/somnia.js）

```
npx somnia chat "你好"
      │
      ▼
  检测 Python3/Python
      │
      ├─ 未找到 → 友好提示安装 Python
      │
      ▼
  检测 pip 包 somnia
      │
      ├─ 未安装 → 自动 pip install somnia
      │
      ▼
  执行 python -m open_somnia chat "你好"
```

## 发版后验证

```bash
# 直接运行（不安装）
npx somnia --help

# 全局安装
npm install -g somnia
somnia --help

# 查看包信息
npm info somnia
```

## 常见问题

### 1. 包名已占用

```bash
npm ERR! 403 Forbidden - PUT https://registry.npmjs.org/somnia
```

**解决**：换一个包名，或联系占用人转让。

### 2. 需要登录

```bash
npm ERR! 401 Unauthorized
```

**解决**：`npm login`

### 3. 版本号已发布

```bash
npm ERR! 403 Forbidden - PUT https://registry.npmjs.org/somnia
You cannot publish over the previously published versions.
```

**解决**：递增版本号。

### 4. 两步验证（2FA）

如果 npm 账号开启了 2FA，发布时需要 OTP：

```bash
npm publish --access public --otp=<验证码>
```
