# AstrBot Harbor 插件

将 Harbor adapter、无界步数的评测执行和 Phoenix trace 上报放在独立插件中，使用原版 AstrBot，无需评测专用分支或核心补丁。

代码仓库：[AstrBotDevs/astrbot_plugin_harbor](https://github.com/AstrBotDevs/astrbot_plugin_harbor)。配套工作台：[Harbor Studio](https://github.com/AstrBotDevs/harbor-studio)。两个仓库均为私有仓库，需要相应访问权限。

## 安装与更新

插件目录为 `AstrBot/data/plugins/astrbot_plugin_harbor/`，有独立 Git 仓库。正常加载只依赖 AstrBot，不启动评测、不修改聊天行为，也不安装 Harbor / Phoenix。这些能力由独立评测进程按需启用。

更新 AstrBot 后，新评测直接打包该 checkout 的最新源码；不需要重新应用补丁。插件自己的代码独立更新。当前已针对 AstrBot 4.28.1 和 Harbor 0.23.0 验证；metadata 中的版本范围不代表未来版本全部经过验证。

Harbor 命令所在的 Python 环境需要安装 `requirements-harbor.txt`。AstrBot checkout 可以包含 `uv.lock`；没有锁文件时，在评测容器中解析依赖，并保存 `agent/dependencies.lock`。Phoenix 依赖仅在评测容器启用上报时安装，不写入 AstrBot 的依赖声明。

## Harbor CLI

```bash
export ASTRBOT_SOURCE_DIR="$HOME/AstrBot-1"
export PYTHONPATH="$ASTRBOT_SOURCE_DIR/data/plugins${PYTHONPATH:+:$PYTHONPATH}"
export ASTRBOT_EVAL_API_KEY="your-api-key"
export ASTRBOT_EVAL_BASE_URL="https://your-provider/v1"

harbor run -p /path/to/harbor-task \
  -a astrbot_plugin_harbor.agent:AstrBotAgent \
  --ak "source_dir=$ASTRBOT_SOURCE_DIR" \
  -m openai/your-model
```

Harbor Studio 已支持独立 adapter 包和 GitHub commit：在运行页选择仓库及版本，无需设置本地 AstrBot 路径。直接调用 Harbor CLI 时仍可传入本地 `source_dir`。adapter 会打包 Git 跟踪的 AstrBot 源码（包括当前文件修改）、本地 `uv.lock` 和明确列出的插件运行文件；不会复制宿主机配置、数据库、其他插件、Git 历史或缓存。依赖在每个 Harbor 容器内安装。源码包 SHA-256 写入结果用于关联本次运行。

每题启动独立进程，在临时 `ASTRBOT_ROOT` 中通过 AstrBot 原生插件加载器加载本插件。评测使用完整本地工具权限、禁用平台连接和 cron 工具；这些设置只作用于该一次性实例。宿主机的 AstrBot 配置不变。Harbor 控制超时、容器清理与评分。当前只支持 local agent runner，不支持题目 MCP servers。

## Phoenix

设置以下环境变量，或在 Harbor Studio 中填写同名连接信息：

```bash
export PHOENIX_COLLECTOR_ENDPOINT="http://host.docker.internal:6006/v1/traces"
export PHOENIX_PROJECT_NAME="harbor-studio"
# PHOENIX_API_KEY is optional.
```

Collector 地址必须能从题目容器访问；Colima 可使用 `host.lima.internal`。每题产生一个 AGENT 根 span、逐次 LLM span、逐次 TOOL span，并记录模型、输入输出、token 用量和 trial ID。`result.json` 包含 trace ID，可由工作台关联 Phoenix 页面。密钥通过环境传递，不放入命令行或源码包。

Tracing 只包装该评测实例的 provider 和 tool executor，退出时恢复，包含取消与异常路径。普通聊天不会自动上报。Phoenix SDK 导出失败通常只记录日志，不参与题目 reward；依赖缺失或 tracing 初始化失败会作为执行错误暴露。

## 维护边界

- `agent.py`：Harbor 的 `BaseInstalledAgent` 接口、源码打包与安装。
- `run_agent.py`：原版 AstrBot 的 lifecycle、`build_main_agent`、配置和输出适配。内部接口变化时主要检查这里。
- `main.py`：AstrBot `Star` 插件；以 `runner.step()` 驱动执行，不需要修改 `step_until_done()`。
- `tracing.py`：Phoenix 与原生 provider / executor 的实例级包装。

这消除了 AstrBot fork，但内部 agent 接口和 tracing 字段变化仍可能需要更新插件。插件不会静默回退到另一个 agent，也不承诺跨所有版本免维护。

## 测试

在插件目录运行。使用临时数据目录，不读取真实凭据。

```bash
# With Harbor installed in this Python environment:
python -m pytest tests/test_harbor_adapter.py -q

# Use AstrBot's environment, with pytest and requirements-tracing.txt installed:
ASTRBOT_SOURCE_DIR=/path/to/AstrBot /path/to/AstrBot/.venv/bin/python \
  -m pytest tests/test_harbor_local_runner.py tests/test_harbor_tracing.py -q

ruff format .
ruff check .
```

本地集成测试使用可控模型 endpoint，但运行真实 AstrBot 初始化、原生插件加载、文件 / Shell / Python 工具与 OTLP 接收器；另有超过 30 轮工具调用、trace 异常恢复、源码包隔离和凭据传递测试。

## License

AGPL-3.0-or-later，与复用的 AstrBot 评测代码一致，见 `LICENSE`。

## 独立产品集成

`studio-agent.json` 是 Harbor Studio v1 adapter 清单。工作台可将本插件打包成独立 adapter，固定内容 hash 后再传给 Harbor，不依赖开发者本机的 AstrBot 仓库。运行结果额外输出通用 `agent/studio-result.json`（schema_version=1），包含标准 token counters 和 trace 信息；Harbor context 同时带有 `metadata.studio`。原生 `result.json` 和日志继续保留。

```bash
# From Harbor Studio, explicitly install this plugin release:
uv run python scripts/install_adapter.py /path/to/plugin/studio-agent.json --package /path/to/plugin
```

已有运行保留原 adapter 快照，更新插件不会改变其执行代码。未提交依赖锁文件的 GitHub commit 会在容器中解析依赖，源码固定不代表所有第三方包版本也已固定。

## Restricted-network installation

Operators can set `ASTRBOT_HARBOR_RUNTIME_ARCHIVE` in the Harbor process environment to a trusted, read-only gzip tar archive containing `bin/uv` and `python/bin/python3.12` (with the complete relocatable Python distribution). The binaries must match the task container's OS, architecture and libc. The adapter uploads this archive into each isolated container and disables Python downloads. Keep archives under immutable versioned names; their SHA-256 is recorded in `agent/setup.log`. No AstrBot installation or checkout is required on the host. Without this setting, the adapter uses the public uv installer with bounded downloads.

Set `ASTRBOT_HARBOR_PYPI_INDEX` to an operator-approved Python package index, e.g. `https://mirrors.aliyun.com/pypi/simple`. This applies to both AstrBot and Phoenix dependency installation. Committed `uv.lock` files remain frozen and may reference their original package URLs. The adapter writes each installation stage and package-manager output to `agent/setup.log`, including on setup failure.

For Ubuntu task images, `ASTRBOT_HARBOR_UBUNTU_MIRROR=http://mirrors.aliyun.com/ubuntu` replaces the official archive/security URLs before installing system dependencies. Other distributions are left unchanged; Ubuntu package signature verification remains enabled.
