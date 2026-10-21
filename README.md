# 深圳大学校园网保活

支持 Windows 和 Linux 的深圳大学校园网保活项目，兼容宿舍区 Dr.COM 和教学区深澜认证。Linux 教学区使用学校官方 `portal`，宿舍区认证与在线状态查询复用公共模块。

| | Windows 桌面版 | Linux 版 |
| --- | --- | --- |
| 界面 | Tkinter 窗口与系统托盘 | 应用图标打开网页面板，也支持 SSH 隧道访问 |
| 入口 | `outputs/campus_keepalive.py` | `linux/campus_linux.py` |
| 后台与自启 | 系统托盘、用户登录启动项 | systemd 用户服务、Linger 开机运行 |
| 密码保存 | Windows DPAPI | Fernet |

## 功能

- 自动识别教学区与宿舍区，也可手动选择认证区域。
- 显示登录状态、系统时间、下次检查时间和运行日志。
- 保活间隔为 0.5～1440 分钟，默认 15 分钟。
- 其他账号在线时继续保活，按同一间隔等待检查；对方离线后再登录，不注销或替换其他账号。
- 开机启动状态与复选框，支持开始、停止和立即检查。
- Windows 托盘运行，Linux 后台服务与桌面入口。
- 带时间戳的运行日志和日志轮转。

## Windows 环境与启动

需要带有 `tkinter` 的 Python。已在 Windows、Python 3.12.7 环境中验证。

在项目根目录打开 PowerShell：

```powershell
python .\outputs\campus_keepalive.py
```

也可以双击 `outputs/启动校园网保活.vbs`。启动文件优先使用 `D:\Anaconda` 中的 Python，否则使用 `pyw -3` / `py -3`；其他安装位置可直接使用上面的命令，或调整启动文件。

已有保存的账号密码时，首次使用本版本会自动开始保活；以后普通启动和开机自启都会恢复上次的保活开关。缺少账号或密码时只在日志里提示，填写后点击“开始保活”即可保存并开始。

勾选“开机自动保活”会保存当前设置，并立即开启保活。点击“停止保活”会记住关闭状态；直接退出程序不会关闭记住的开关。需要覆盖上次的停止状态时，也可使用 `python .\outputs\campus_keepalive.py --start`。

| 操作 | 用途 |
| --- | --- |
| 窗口右上角 × | 隐藏到托盘 |
| 托盘“显示窗口” | 恢复界面 |
| “停止保活” | 停止定时检查，并在下次启动时保持停止 |
| 勾选“开机自动保活” | 保存设置、开启自启，并立即开启保活 |
| 托盘或“程序”菜单的“退出程序” | 退出进程并保留保活开关，下次启动按原状态恢复 |

移动项目后，在新位置重新设置开机启动。启动遇到问题时使用 `outputs/排错启动.bat` 查看错误。

完整操作见 [Windows 使用说明](outputs/使用说明.md)。

## Linux 环境与启动

学校官方说明与下载入口：**[教学办公区Linux用户脚本认证指南（深圳大学信息中心）](https://www1.szu.edu.cn/nc/view.asp?id=639)**。

按 [Linux 使用说明](linux/README.md) 准备 Python 3.10+、依赖和官方 `portal` 客户端。官方客户端适用于 Linux x86-64。

Linux 桌面用户在完整仓库根目录，以当前用户执行：

```bash
python3 linux/campus_linux.py install --desktop --open
```

以后在应用菜单搜索 **SZU 校园网保活**，或双击桌面快捷方式，即可打开面板。首次填写账号、密码和间隔，保存后点击“开始保活”；需要自启时勾选复选框。桌面快捷方式首次启动可能需要右键选择“允许启动”。

无图形桌面的主机使用命令行：

```bash
python3 linux/campus_linux.py portal-info
python3 linux/campus_linux.py configure
python3 linux/campus_linux.py install --enable --start
```

后台服务需要 systemd 用户管理器，免登录开机运行需启用 Linger。默认面板地址为 `http://127.0.0.1:8765`；远程访问、日常控制和卸载命令见 [Linux 使用说明](linux/README.md)。

## 项目结构

```text
szu-campus-keepalive/
├── README.md
├── .gitignore
├── .gitattributes                # 跨平台换行约定
├── .github/workflows/tests.yml   # Windows / Ubuntu 测试
├── outputs/
│   ├── campus_keepalive.py       # Windows 界面与程序入口
│   ├── campus_core.py            # 公共认证、配置与调度
│   ├── campus_tray.py            # Windows 系统托盘
│   ├── 启动校园网保活.vbs
│   ├── 排错启动.bat
│   └── 使用说明.md
├── linux/
│   ├── campus_linux.py           # 后台、面板与命令行入口
│   ├── auth_linux.py             # 官方客户端适配与调度
│   ├── platform_linux.py         # 配置与 systemd 自启
│   ├── desktop_linux.py          # 桌面入口与面板连接
│   ├── requirements.txt
│   ├── README.md
│   ├── web/                      # 网页界面
│   └── tests/                    # Linux 与公共模块测试
└── tests/                        # 公共模块与 Windows 界面测试
```

安装时保留完整仓库结构，Linux 会使用 `outputs/campus_core.py`，Windows 的三个应用 Python 文件需位于同一目录。

## Git / GitHub 跨平台维护

两个平台放在同一个仓库、同一条主分支，按操作系统选择入口。公共状态查询和宿舍区认证在 `outputs/campus_core.py`；Linux 教学区适配、界面、配置与自启在 `linux/`；Windows 界面和托盘在 `outputs/`。

安装 `linux/requirements.txt` 后运行跨平台回归测试：

```bash
python -B -m unittest discover -s linux/tests -v
```

GitHub Actions 使用 Windows / Ubuntu、Python 3.10 / 3.12 测试矩阵。推送后在仓库的 Actions 页面查看结果。`.gitattributes` 约定 Python 与网页源码使用 LF，Windows 启动文件使用 CRLF。

## 配置与日志

Windows 运行数据位于 `outputs/data/`：

- `settings.json`：账号、加密密码、区域与间隔。
- `run_state.json`：上次的保活开关，不包含账号密码。
- `keepalive.log` 及轮转文件：运行日志。

Linux 配置位于 `~/.config/szu-campus-keepalive/`，日志和状态位于 `~/.local/state/szu-campus-keepalive/`。完整文件列表见 [Linux 配置与日志](linux/README.md#配置与日志)。`.gitignore` 已配置运行数据、缓存、截图和虚拟环境的排除规则。

手动查询真实认证状态可运行 Windows 的 `tests/smoke_campus_tray.py`，或 Linux 的 `python3 linux/campus_linux.py probe`。


