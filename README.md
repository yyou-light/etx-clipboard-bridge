# etx-clipboard-bridge

通过 **ETX 与 Windows 同步的剪贴板**，让 AI 助手与 ETX 内部的 Linux 环境交互：收发文件、运行仿真、跟进状态和日志，并在配置好波形工具后查询波形。所有操作复用已有 ETX 连接，不需要额外开放网络端口。公开的 ETX 桥接器单文件入口支持「剪切板」（X11）和可配置的 GitHub Actions Runner。

**前提是 ETX 支持并已开启 Windows 与远端之间的双向文本剪贴板同步。** 保持已有 ETX 桌面打开，日常收发和任务跟进可以交给 AI。仿真和波形解析由 ETX 内部已有的工具完成；本项目负责把请求送进去、把结果带回来。

已有安装时，可以让 AI 更新 Windows 客户端和 ETX 接收端，再核对两端实际运行的版本。站点没有远端维护入口时，按[双端升级说明](docs/OPERATIONS.md#双端升级)交接一次发布包。

## 单文件读取公共入口

从任意工作区调用 `etx_bridge.py`。站点配置提供已配对的剪切板或用户自己的 Runner 仓库与工作流，项目配置提供获准读的路径。原请求的 `resume` 沿已记录的通道取回执。

[单文件配置与命令](docs/CONFIGURATION.md#单文件读取) · [通道路由与回执](docs/ROUTING.md) · [自建 Runner 只读入口](docs/CONFIGURATION.md#自建-runner-只读入口)

## 用户需要做什么

### 第一次使用

准备好 Windows 和 Linux 上的 Python 3.10+、Windows PowerShell、OpenSSL，以及已开启文本复制粘贴的 ETX 桌面。跑仿真还需要自己的工程、LSF 队列和 `simmer` 环境。

在仓库目录中按以下顺序操作一次，详细步骤见[首次安装说明](docs/QUICKSTART.md)：

1. Windows 运行 `python scripts/configure.py windows`，按提示填写自己的 Linux 账号、安装目录、工程目录和工具路径。
2. 将生成的 `.private/bootstrap/etx-clipboard-bridge-setup.zip` 用现有文件传递方式放到 Linux 安装目录并解压。这个包带个人配置，不要上传到公共仓库。
3. 在 Linux 解压目录运行 `python3 scripts/configure.py linux`，记下输出的 `SERVER_FINGERPRINT`。
4. 在 ETX 桌面的终端运行 `python3 scripts/start.py endpoint`，看到 `ready` 后，在 Windows 运行 `python scripts/start.py pair <SERVER_FINGERPRINT>`。
5. 按需启动下面的 Worker，可以只开一路。

### 日常启动或 ETX 重启后

在已有 ETX 桌面的终端进入 Linux 安装目录，启动收发服务：

```bash
python3 scripts/start.py endpoint
```

看到 `ready` 后，这个启动终端可以关闭，**ETX 桌面要保持打开**。

需要几路并行，就分别在几个终端中启动不同编号。**每条命令使用独立终端，Worker 终端保持运行**：

```bash
python3 scripts/start.py worker work01
python3 scripts/start.py worker work02
python3 scripts/start.py worker work03
python3 scripts/start.py worker work04
python3 scripts/start.py worker work05
```

已经运行的编号不用再开。Worker 可以位于不同 ETX 节点，只要能访问同一工程和任务目录。若 `python3` 不是配置时选择的版本，换成该 Python 的完整路径。

### 平时怎样交给 AI 使用

让能执行本地命令的 AI 助手（例如 Codex）打开本仓库，并读取 `AGENTS.md`。之后直接描述任务，例如：

- “读取这个文件：`/path/to/project/test.sv`。”
- “把这份修改上传到指定文件。”
- “用 work01 跑 demo_case，seed 0，结束后分析日志；失败先 debug，不重跑。”
- “同时跑这五个 case，各占一路 Worker。”
- “查看这次仿真的波形：列出信号层级，读取指定时刻的值，再检查这一时间段的变化。”

AI 负责 Windows 侧收发、派单和结果查询，你不需要手动搬运每次任务的文件或复制通信内容。仅要求查看文件、改代码或分析日志时，不会自动启动仿真。命令行用法见[快速上手](docs/QUICKSTART.md)。

## 文件大小限制与日志保留

| 类型 | 单文件上限 | 单次范围 |
| --- | --- | --- |
| 普通 UTF-8 文本读取、上传（日志、源码等） | **256 MiB（268435456 字节）** | 最多十个精确文件，单批总计最多 256 MiB |
| 支持的 PDF／Office 二进制文件下载 | **16 MiB** | 每次一个精确文件，只读 |
| 日志首错／末尾摘录 | 原日志最多 256 MiB | 每次返回正文最多 256 KiB |

新上传默认用 **32 KiB／块**，读取每块最多 **256 KiB**。每块和整文件都校验 SHA256，中断后可沿用原请求续传。上传保留基线摘要、事务备份、安装后校验和恢复能力。二进制上传、目录、符号链接和通过文件接口下载 FSDB 均不支持。

日志优先用 `native-log` 只取首错上下文和结束摘要。必须下载完整日志时，仅临时用于本次分析；处理完成后，AI 保存关键摘录、结论、源路径和校验信息，再用同操作号的 `cleanup-logs` 清理已核实的本地完整日志。成功下载会移除临时分块，待恢复的传输暂存。该清理不删除远端原始日志、源码基线或上传备份。

只读取已授权的精确路径。`native-log` 使用现有配置读根下的 `stdout.log`／`stderr.log`，不扫描目录；支持默认首错上下文，以及 `--mode tail`、`--mode range --offset <字节偏移>`。扫描超时或摘录受限会明确标记，不把未扫描完误报为没有错误。

## 跑仿真、看波形还需要什么

- **跑仿真**：ETX 内部需要已有可用的工程、仿真工具和许可证。当前仿真入口对接 `LSF + simmer`，最多五路 Worker 并行；其他工程运行方式需要适配。
- **看波形**：ETX 内部需要已经部署与本项目兼容的 **xverify／xverif 等波形查询工具**，以及它所需的 Verdi 环境和许可证。配置完成后，AI 可以查询信号层级、指定时刻的值和一段时间内的变化。已有工具及其许可证由使用者提供。

波形解析在 ETX 内部进行，Windows 收到查询结果，无需通过剪贴板传回整份波形文件。波形查询也会使用一个匹配的空闲 Worker；正在生成的波形需等待仿真结束后再查，不需要你另外打开常驻的 Verdi 窗口。

## Worker 多久接单、多久查一次状态

| 操作 | 默认频率 | 是否使用 Windows 剪贴板 |
| --- | --- | --- |
| AI 跟进正在运行的仿真 | 每 **5 分钟**合并查询一次；空闲不查 | 查询时使用 |
| 空闲 Worker 检查新任务 | 约每 **2 秒** | 不使用，只访问 Linux 本地任务文件 |
| 忙碌 Worker 更新在线状态 | 约每 **8 秒** | 不使用，只更新 Linux 本地记录 |

因此，新任务不用等 5 分钟才接单。5 分钟是 AI 跟进当前仿真的查询间隔，需要 AI 在该任务中持续跟进；启动 Worker 本身不会启动一个永久巡检的 AI。

## 正常复制粘贴会不会冲突

**会共用剪贴板。** 同时发出的请求会在 Windows 上排队，依次收发。ETX 上的 Worker 仍可并行处理任务。

图片和文字使用同一套 **30 秒保护**。检测到复制后，通信会退让，每次再复制都重新计时。连续 30 秒没有新变化后，原请求自动继续。普通 Windows 截图可以在收发结束后恢复。

连续复制使单次通信的 **5 分钟**等待预算用尽时，请求会退出。若已派发仿真，AI 会沿原任务号查结果。保护根据复制变化计时，粘贴时若碰上通信内容，可以重新复制。

## 每个 Worker 的编译和结果目录怎么区分

**本节仅用于我们内部与现有 flow 绑定的实现，外部用户可忽略。** 自有工程的编译缓存、目录命名和结果定位，应以自己的仿真 flow 及适配配置为准。

五路共用配置的工程源码，每路同时只处理一个任务。派单时向 `simmer` 传入对应的目录后缀：

```text
work01 → --dir-suffix work01
work02 → --dir-suffix work02
……
work05 → --dir-suffix work05
```

日志、波形等结果目录按下面的规则定位：

```text
<结果根目录>/<target>__<simulator>__<case>__<seed>_<Worker编号>/
```

例如，同一个 `demo_case`、seed `0` 分别交给两路，默认配置下的结果目录为：

```text
<结果根目录>/sys_tb__vcs__demo_case__0_work01/
<结果根目录>/sys_tb__vcs__demo_case__0_work02/
```

编译文件和编译缓存的具体位置、复用方式，由工程自身的 `simmer` 流程决定；etx-clipboard-bridge 负责传入不同后缀，不另建五份工程。同一路重跑相同 case 和 seed 时，结果目录名相同；需要保留旧结果时，先备份再重跑。不要同时启动两个同编号 Worker。

完整安装步骤见[快速上手](docs/QUICKSTART.md)，账号和目录设置见[配置说明](docs/CONFIGURATION.md)，常见错误见[问题处理](docs/OPERATIONS.md)。
