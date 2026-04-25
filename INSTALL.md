# 安装说明

## 方式一：用户级安装（推荐）

解压到：

```
~/.workbuddy/skills/technical-paper-translation/
```

加载方式：由 WorkBuddy / CodeBuddy 的 skill 机制自动识别。

## 方式二：项目级安装

解压到项目根目录下：

```
<project>/.workbuddy/skills/technical-paper-translation/
```

仅对当前项目生效。

## 依赖安装

```bash
cd ~/.workbuddy/skills/technical-paper-translation
python3 -m pip install -r requirements.txt
```

首次运行时，Marker 会下载 ~1–2GB 模型权重到本地缓存。

## 验证

```bash
python3 scripts/run.py --help
```

应当看到参数帮助信息。

## 版本

v0.1.0
