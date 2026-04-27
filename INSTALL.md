# 安装说明

本 skill 的核心运行方式是 **Python 脚本 + 文件协议**，不绑定 WorkBuddy、OpenClaw 或任何特定 Agent 平台。任意平台只要能执行 Python、读取 prompt 文件、调用 LLM 并写回译文文件，都可以集成。

## 方式一：通用本地安装（推荐）

解压或克隆到任意目录，例如：

```bash
/path/to/qiq-tech-paper-trans/
```

之后通过脚本绝对路径或进入目录后运行：

```bash
cd /path/to/qiq-tech-paper-trans
python3 scripts/run.py --help
```

## 方式二：宿主平台安装

如果宿主平台有自己的 skill / tool 目录，可放入对应目录。例如：

```bash
# WorkBuddy / CodeBuddy 示例
~/.workbuddy/skills/qiq-tech-paper-trans/

# 项目级 WorkBuddy / CodeBuddy 示例
<project>/.workbuddy/skills/qiq-tech-paper-trans/

# OpenClaw 或其他平台
<platform-skill-dir>/qiq-tech-paper-trans/
```

平台只需要把 `entrypoint` 指向 `scripts/run.py`，并按照 `SKILL.md` 中的文件协议执行 prepare、翻译写回、finalize 三步。

## 依赖安装

```bash
cd /path/to/qiq-tech-paper-trans
python3 -m pip install -r requirements.txt
```

首次运行时，Marker 会下载 ~1–2GB 模型权重到本地缓存。

## 验证

```bash
python3 scripts/run.py --help
```

应当看到参数帮助信息。

## 版本

v0.2.6
