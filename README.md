# 深圳大学校园网保活

一个使用 Python 标准库编写的 Windows 桌面程序，兼容深圳大学宿舍区 Dr.COM 和教学区深澜认证。通过认证接口检查当前账号的在线状态，在需要时登录，不依赖 Selenium、Chrome 或 ChromeDriver。

## 功能

- 图形界面显示最近一次登录状态、系统时间、下次检查时间及认证响应。
- 自动识别两种认证入口，也可以手动选择认证区域。
- 保活间隔可设置为 0.5～1440 分钟，默认 15 分钟。
- 支持当前 Windows 用户登录后自动启动，界面可查看和切换启动项状态。
- 点击窗口右上角 × 后退到系统托盘，已开启的保活继续运行。
- 使用 Windows DPAPI 保存密码；日志轮转，认证响应进行脱敏。

本项目不是学校官方客户端。它做的是定时检查与必要时重新登录，不能保证永不掉线，也不能在电脑休眠、关机或网络不可用时继续保活。

## 环境与启动

当前版本已在 Windows、Python 3.12.7 环境中运行验证。Python 需要带有 `tkinter`，没有需要通过 pip 安装的第三方依赖。

在项目根目录打开 PowerShell：

```powershell
python .\outputs\campus_keepalive.py
```

也可以双击 `outputs/启动校园网保活.vbs`；如果看不到窗口，用 `outputs/排错启动.bat` 查看错误。启动文件保持原样：优先使用 `D:\Anaconda` 中的 Python，否则使用 `pyw -3` / `py -3`。如果你的 Python 安装位置或命令不同，直接使用上面的 `python` 命令，或调整自己副本中的启动文件。

首次运行需要输入自己的校园网账号、密码，保存后点击“开始保活”。普通双击启动只做一次状态检查，不会自动开始定时保活；Windows 启动项启动则会自动开始。

- 点 ×：隐藏到托盘，继续已经开启的保活。
- 托盘“显示窗口”：恢复界面。
- “停止保活”：停止定时检查，不注销校园网。
- 托盘或“程序”菜单中的“退出程序（停止保活）”：彻底退出。

同一 Windows 会话中只允许运行一个保活实例。原位置的程序还在运行时，启动这份副本会尝试恢复原窗口，不会另外启动第二份。若要迁移使用新位置，应先自行退出旧实例，在新位置重新输入账号，并重新设置开机启动。

完整操作说明见 [使用说明](outputs/使用说明.md)。

## 项目结构

```text
szu-campus-keepalive/
├── README.md
├── .gitignore
├── outputs/
│   ├── campus_keepalive.py       # 窗口、状态展示与程序入口
│   ├── campus_core.py            # 认证、配置、启动项与保活调度
│   ├── campus_tray.py            # Windows 原生系统托盘
│   ├── 启动校园网保活.vbs
│   ├── 排错启动.bat
│   └── 使用说明.md
└── tests/
    ├── test_campus_keepalive.py   # 认证与调度离线测试
    ├── test_campus_gui.py         # 界面行为测试
    ├── test_campus_tray.py        # Windows 原生托盘测试
    └── smoke_campus_tray.py       # 可选的真实网络只读检查
```

保留 `outputs/` 与 `tests/` 的原有相对位置，启动文件、源码和测试无需改路径。三个应用 Python 文件必须放在同一目录。

## 配置与隐私

本项目副本只包含源码、启动文件、说明和测试，不包含个人账号配置、日志或截图。程序首次保存或运行时会自行生成 `outputs/data/`。

- `settings.json`：账号与加密后的密码等本机设置。
- `keepalive.log` 及轮转文件：运行日志。
- `last_response.json`：最近一次认证响应，可能包含账号和 IP。

`.gitignore` 已排除上述运行数据、测试截图、缓存、虚拟环境和常见本地配置文件。即使密码经过加密，也不要将个人配置提交到仓库。发布截图、问题反馈或新增文件前，仍应检查是否包含账号、密码、IP、MAC、Cookie 等私人信息。

## 测试

原生界面和托盘测试需要可用的 Windows 桌面。先退出所有正在运行的保活实例，再在项目根目录执行：

```powershell
python -B -m unittest discover -s tests -v
```

默认测试使用模拟认证数据，不进行真实登录或注销；需要退出实际程序，是为了避免单实例互斥锁及托盘窗口名称与测试冲突。

`tests/smoke_campus_tray.py` 是单独运行的可选检查，会读取本机已保存的配置并访问校园网认证接口；它只检查在线状态，不提交登录、不修改开机启动。它不包含在上面的默认测试命令中。

## 首次推送到 GitHub

这个文件夹只是独立副本，未自动初始化 Git、创建提交、绑定远程仓库或上传。先在 GitHub 创建空仓库，再在本项目根目录打开 PowerShell：

```powershell
git init -b main
git rev-parse --show-toplevel
```

确认输出是 `szu-campus-keepalive` 项目本身，而不是桌面或用户主目录，再准备提交：

```powershell
git add .
git diff --cached --stat
git diff --cached
```

确认待提交内容不含个人数据后，将下面的地址替换为你自己的仓库地址：

```powershell
git commit -m "Initial commit"
git remote add origin "https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git"
git push -u origin main
```

尚未替作者选择开源许可证；需要发布许可证时，再自行添加 `LICENSE` 文件。
