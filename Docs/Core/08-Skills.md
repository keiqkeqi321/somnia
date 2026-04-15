# Skills

## 概述

Skills（技能）是 Somnia 的可扩展知识库机制，允许用户在工作区或全局目录中放置 Markdown 格式的专业知识文件，通过 `load_skill` 工具按需加载到 Agent 上下文中。

---

## 技能文件结构

技能文件命名为 `skill.md`，放置在以技能名命名的目录中：

```
{skill_name}/
└── skill.md
```

文件内容支持可选的 YAML frontmatter：

```markdown
---
description: 代码格式规范化工具使用指南
author: user
---

## 使用方法
...详细的技能内容...
```

### Frontmatter 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `description` | 否 | 技能描述，显示在技能列表中 |
| 其他 | 否 | 任意 key-value 元数据 |

---

## 搜索路径

`SkillLoader.for_workspace()` 按以下顺序扫描技能目录：

| 优先级 | 路径 | 范围 |
|--------|------|------|
| 1 | `~/.open_somnia/skills/` | 全局 |
| 2 | `{workspace}/skills/` | 工作区 |
| 3 | `{workspace}/.open_somnia/skills/` | 工作区隐藏 |

**同名技能以后扫描的为准**（工作区覆盖全局）。

---

## 技能加载流程

```
load_skill("file-format-normalizer")
        │
        ▼
┌─────────────────────┐
│  SkillLoader.load() │
│                     │
│  1. reload()        │  重新扫描所有技能目录
│  2. 查找 skill      │  按名称（大小写不敏感）
│  3. 返回 XML 包裹体  │  <skill name="...">...</skill>
└─────────────────────┘
```

### 输出格式

```xml
<skill name="file-format-normalizer">
...技能正文内容...
</skill>
```

### 未找到技能时

返回错误信息 + 可用技能列表：

```
Error: Unknown skill 'xxx'. Available: file-format-normalizer, another-skill
```

---

## 技能范围标签

| Scope | 来源路径 |
|-------|---------|
| `global` | `~/.open_somnia/skills/` |
| `workspace` | `{workspace}/.open_somnia/skills/` |
| `workspace-legacy` | `{workspace}/skills/` |
| `custom` | 其他自定义路径 |

---

## 命令

| 命令 | 说明 |
|------|------|
| `/+{skill_name}` | 在 REPL 中快速加载技能 |
| `/skills` | 列出所有可用技能 |

---

## 技能列表渲染

```
- file-format-normalizer [global] - 代码格式规范化工具使用指南
  use: /+file-format-normalizer
  path: /home/user/.open_somnia/skills/file-format-normalizer/skill.md
```

---

## 与 Subagent 的关系

Subagent 的独立 ToolRegistry 也包含 `load_skill` 工具，子代理可以加载技能来增强其能力。

---

## 相关代码

- `open_somnia/skills/loader.py` — `SkillLoader`
- `open_somnia/runtime/agent.py` — `_register_local_tools()` 中注册 `load_skill`
