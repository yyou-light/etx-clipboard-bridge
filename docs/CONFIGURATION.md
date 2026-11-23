# 配置

实际配置位于 `.private/site.json`，Linux 由当前账号持有且权限为 `0600`。公共仓库仅提供 `config/site.example.json`。环境变量 `ETX_X11_SITE` 可以显式指定同样受保护的配置文件。

| 字段 | 含义 |
| --- | --- |
| `remote.user` | Linux 账号，启动时核对当前身份 |
| `remote.install_root` | Linux 中本发布包的准确安装根 |
| `remote.project_root` / `regression_root` | 工程工作目录及原生日志/结果根 |
| `remote.queue_root` / `lock_root` | 此实例自己的队列与锁；所有参与者使用一致的配置 |
| `remote.python` / `openssl` | Linux 工具路径，单个程序名或准确路径，不是 Shell 命令 |
| `remote.environment_script` / `lsf_module` | 可选项目环境脚本和 LSF 模块 |
| `remote.lsf_queue` / `target` / `simulator` | 现有 LSF/simmer 适配的队列、target 与输出命名信息 |
| `remote.fsdb_name` | 本 flow 的 FSDB 文件名，默认 `waves.fsdb` |
| `permissions.read_roots` / `write_roots` | 显式文件范围；写范围必须在读范围内，不能为 `/` |
| `permissions.wave_fsdbs` | 已授权、已完成 FSDB 的准确路径，禁止父目录代替白名单 |
| `wave.enabled` / `root` / `release` | 可选外部后端与准确发布摘要；默认关闭 |
| `wave.compatibility_patch` | `none` 或已验证的 `time-loop-v1`，不能静默适配未知后端 |
| `monitor_interval_seconds` | 跟进运行任务的默认间隔，当前为 `300` 秒 |

文件范围来自操作者自己的配置，不自动开放整个 NFS 或安装/凭据目录。Linux 账号、路径、工具和波形后端须按自己的站点填写；共享目录可见不代表已有派单权限。

配置文件中的 `simulator` 用于现有输出命名，不代表包已支持所有仿真器。业务执行适配目前沿用 LSF + `simmer`；其他 flow 要单独适配并验收。

Windows 和 Linux 的私钥分别生成，不打入公共发布包。Windows `.private/` ACL 限制到当前身份及 SYSTEM；Linux 配置、身份和队列使用当前用户的私有权限。

波形适配要求使用者已有兼容的 xverif 后端、LSF 和许可，且配置两端相同的准确 FSDB 清单。后端的厂商软件、库和许可证均不在本仓库。来源部署验收的是 `verdi/Y-2026.03-1S`；实际版本由外部后端配置，本工具不会自动切换，其他版本须另行验证。

## 单文件读取

公开入口从安装目录加载读取客户端。命令接受两份私有配置，可以从其他项目目录调用。

| 配置 | 填写内容 |
| --- | --- |
| [读取站点模板](../config/read-site.example.json) | 用 `clipboard` 启用已配对的剪切板。Runner 可设为 `null`，或填写 `repository`、分支 `ref`、工作流文件名 `workflow` 和接收证书及私钥的绝对路径。默认从 PATH 找已登录的 GitHub CLI 和 OpenSSL，也可显式指定 `gh`、`openssl` 的路径。 |
| [读取项目模板](../config/read-project.example.json) | 填项目 `id`、结果与请求记录目录 `state_dir`、获准读的目录 `read_roots` 和精确文件 `read_paths`。每次读取和续接都核对该项目范围。接收端与 Runner 工作流仍执行其已部署的授权检查。 |

剪切板复用安装目录的 `.private/x11/session/client.json` 及现有配对。Runner 使用兼容的
ETX Secure Read 工作流。该工作流接受 `request_id`、`resource_type=user_requested_file`、
`resource_id`、`include_assets`、`explicit_user_request`，按请求号标识 Run，
上传 `etx-secure-read-<Run ID>` 加密 Artifact，并在原 Run 提供 `CIPHERTEXT_SHA256`。

```powershell
python C:/path/to/etx-clipboard-bridge/etx_bridge.py --site C:/private/read-site.json --project C:/project/bridge-project.json read /nfs/replace-me/project/sample.txt --transport 剪切板
python C:/path/to/etx-clipboard-bridge/etx_bridge.py --site C:/private/read-site.json --project C:/project/bridge-project.json resume <原请求号>
```

`plan` 只查看选路。单文件默认混合模式，可显式传 `Runner`、`剪切板`
（兼容 `x11` 和 `剪贴板`）或 `offline`。小文本优先剪切板，预计至少
4 MiB 或 PDF/Office 走可用的 Runner。用 `--expected-bytes` 提供已知大小。
只有确认剪切板尚未发送且符合分流条件时才切换。出错时保留原请求号并执行 `resume`。
Runner 查原 Run，剪切板按原请求取结果，两路都核对本地结果长度与 SHA256。
读取输出是 JSON，含本地文件、原请求号和回执路径。读取链路按文件操作，仿真和波形使用现有入口。

### 自建 Runner 只读入口

Linux Runner 需要 Python 3 和 OpenSSL。使用自己的 GitHub 仓库及 self-hosted Runner。
将 [工作流模板](../templates/runner/etx-secure-read.yml) 放到该仓库的 `.github/workflows/`。
在 Linux Runner 所在站点安装 [读取程序](../templates/runner/secure_read.py)，再按
[Linux 读取配置模板](../templates/runner/secure-read.example.json) 填写准入范围和公钥。

| 配置位置 | 要填的值 |
| --- | --- |
| GitHub 仓库变量 | `ETX_BRIDGE_READ_HELPER` 填 Linux 上读取程序的绝对路径，`ETX_BRIDGE_READ_CONFIG` 填 Linux 私有 JSON 的绝对路径。 `ETX_BRIDGE_READ_RUNNER_LABEL` 可指定自托管 Runner 标签，默认为 `etx-read`。 |
| Linux 私有 JSON | 用当前 Runner 账号持有的 `0600` 文件。设准入目录 `read_roots`、精确文件 `read_paths`、站点 `lock_root` 和接收者**公钥证书** `recipient_cert`。锁目录为当前账号持有的 `0700` 目录。与剪切板服务共用工程时，填同一个源码锁目录。 |
| Windows 站点 JSON | 填自己的仓库、分支、工作流文件名和解密证书及私钥路径。私钥保存在该站点的私有目录。 |

Linux 侧接收单个授权的普通文本或 PDF、Office 文件，验证精确 NFS 路径并持共享读锁。
工作流按原请求号产生 Run，在 GitHub 返回加密结果和密文摘要。本机用原请求 `resume` 回查。
安装和现场确认使用无业务数据的合成文件。站点身份资料和实际授权配置进入用户自己的私有目录。
