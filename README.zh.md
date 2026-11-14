# <img src="https://raw.githubusercontent.com/google/adk-docs/main/docs/assets/agent-development-kit.png" alt="Agent Development Kit Logo" width="30"> ADK 示例方案 (ADK Recipes)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

[ [English](README.md) | 简体中文 ]

欢迎使用。这是 ADK 示例方案（Recipes）的公开集合 —— 包含一系列轻量、可直接运行的智能体，演示如何使用[智能体开发套件（Agent Development Kit，ADK）](https://adk.dev)解决实际业务问题。您可以 Fork 其中的方案作为您自己项目的起点，也可以浏览这些代码集合来学习智能体架构设计范式。

无论您是要构建智能客服机器人、科研助理智能体，还是某种全新的系统，这里的示例方案都能为您提供现成可用的代码基石，告别从零起步。

## 体验示例方案 (Try a recipe)

示例方案分别存放在以下三个目录中：

- **[`core/`](./core/)** —— 由 `agents-cli` 团队精心整理的权威设计范式。轻量、聚焦的示例，专门深入讲解单一技术点（如 OAuth 认证流程、会话记忆、安全护栏、RAG 检索增强生成范式等）。
- **[`contrib/`](./contrib/)** —— 社区贡献及垂直行业的可部署智能体（例如 `contrib/python/retail-product-search`、`contrib/python/retail-virtual-tryon`），可一键部署至 Gemini Enterprise Agent Platform（Agent Engine）、Gemini Enterprise (GE)、Cloud Run 以及 Agent Garden。
- **[`plugins/`](./plugins/)** —— 特定领域的垂直技能插件（例如 `plugins/retail/skills/product-search`、`plugins/retail/skills/virtual-tryon`），以可安装的 AI 编码助手 / GE Desktop 技能形式提供（包含 `SKILL.md` 与 `EVAL.yaml`）。

每个示例方案均包含独立的 `README.md`，提供专属的安装配置与运行说明。

## 前置要求 (Prerequisites)

请参考 [ADK 快速上手指南](https://adk.dev/get-started) 安装 ADK。各语言 SDK 如下：

- [ADK Python](https://github.com/google/adk-python)
- [ADK TypeScript](https://github.com/google/adk-js)
- [ADK Go](https://github.com/google/adk-go)
- [ADK Java](https://github.com/google/adk-java)
- [ADK Kotlin](https://github.com/google/adk-kotlin)

## 贡献示例方案 (Contributing a recipe)

请先从[示例方案核对清单 (Recipe Checklist)](./docs/recipe-checklist.md)开始 —— 一页清单即可涵盖发布方案所需的一切要求。若您需要完整的背景信息与工具参考，[示例方案手册 (Recipe Handbook)](./docs/recipe-handbook/README.md) 提供了详尽的深入细节。

**仓库内置技能（Repo skills）** —— 用于协助构建本仓库的 AI 编码助手辅助工具（包括方案脚手架、清单生成、pyproject 对齐等）存放在 [`.agents/skills/`](./.agents/skills/) 目录中。请勿将其与**插件（plugins）**混淆，插件是位于 `plugins/<vertical>/<solution>/` 目录下直接交付给用户的方案技能。

## 获取帮助 (Getting help)

欢迎在 [github.com/google/adk-recipes/issues](https://github.com/google/adk-recipes/issues) 提交 GitHub Issue —— 无论是报告 Bug、咨询问题，还是提议新的 `contrib/` 示例方案。

## 许可证 (License)

Apache 2.0 —— 详情请参阅 [LICENSE](./LICENSE)。

## 免责声明 (Disclaimer)

这不是 Google 官方支持的产品。不适用于 [Google 开源漏洞奖励计划 (Google OSS Vulnerability Rewards Program)](https://bughunters.google.com/open-source-security)。本仓库中的方案仅供演示及作为开发起点，不可直接用于生产环境。

---

> **翻译说明（Translation Disclaimer）**
> 本文档由社区志愿者（[@JasonYeYuhe](https://github.com/JasonYeYuhe)）人工翻译并维护，旨在降低中文开发者的阅读门槛。最后同步日期：2026年11月14日。若译文与英文原版存在任何出入或滞后，请以 [英文原版 (README.md)](README.md) 为准。欢迎通过 PR 协助改进译文！
