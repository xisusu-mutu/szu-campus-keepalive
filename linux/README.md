# Linux 版：桌面图标、后台保活与管理面板

适用于 Linux 桌面和无图形桌面的主机。教学区登录使用学校官方 `portal`；宿舍区 Dr.COM、接入参数探测和在线状态查询使用 `outputs/campus_core.py`。

学校官方说明与下载入口：**[教学办公区Linux用户脚本认证指南（深圳大学信息中心）](https://www1.szu.edu.cn/nc/view.asp?id=639)**。

## 功能

- 教学区与宿舍区自动识别，支持手动选择区域。
- 点击应用图标，自动启动后台并连接面板。
- 登录状态、系统时间、下次检查时间和运行日志。
- 开始、停止、立即检查 / 登录，间隔为 0.5～1440 分钟。
- 开机启动复选框、systemd 后台服务和暂停状态保存。

## 运行要求

Python 3.10+、`cryptography>=41`、`pexpect>=4.9,<5`。当前适配的官方 `portal` 为 Linux x86-64 程序。已在 Ubuntu 24.04、Python 3.12.3、systemd 255 上验证。

后台与自启需要 systemd 用户管理器、`systemctl`、`systemd-analyze` 和 `loginctl`。桌面入口还需默认浏览器，以及 `xdg-open`（`xdg-utils`）或 `gio`。其他进程管理环境可使用 `run` 命令。

Ubuntu / Debian 安装依赖：

```bash
sudo apt install python3 python3-cryptography python3-pexpect xdg-utils
```

也可以使用虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r linux/requirements.txt
```

使用虚拟环境时，后续命令中的 `python3` 替换为 `.venv/bin/python`。安装器会记录所用解释器的路径。

## 安装学校官方 portal

1. 从上方官方指南下载并解压附件 `portal.rar`。
2. 将 `/path/to/portal` 替换为解压后的文件路径，然后执行：

```bash
install -D -m 700 /path/to/portal "$HOME/.local/lib/szu-campus-keepalive/portal"
```

3. 在仓库根目录检查安装状态：

```bash
python3 linux/campus_linux.py portal-info
```

输出中的 `"ready": true` 表示客户端已就绪。只使用宿舍区时可省略官方客户端。

当前适配版本的 SHA-256：

```text
288555fca83dd8edd3d406dfbc9467640d0fdd4e2989d7d171ba36b82fd67b50
```

## Linux 桌面安装

在完整仓库根目录，以当前用户执行：

```bash
python3 linux/campus_linux.py install --desktop --open
```

命令会安装用户服务和应用入口，启动后台并打开面板。以后在应用菜单搜索 **SZU 校园网保活**，或双击桌面快捷方式即可。快捷方式首次启动可能需要右键选择“允许启动”。

首次打开面板，填写账号、密码和间隔，点“保存并应用”，再点“开始保活”。需要自动启动时勾选启动项复选框。

| 操作 | 用途 |
| --- | --- |
| 点击应用图标 | 启动后台并打开面板 |
| 关闭网页 | 关闭当前面板 |
| “停止保活” | 暂停周期检查 |
| “开始保活” | 恢复周期检查 |
| “退出后台程序” | 停止后台和管理界面 |

命令行打开面板：

```bash
python3 linux/campus_linux.py open
```

该命令会读取后台的实际端口，支持安装时设置的 `--port`。打开失败时，错误记录位于 `~/.local/state/szu-campus-keepalive/last_launcher_error.txt`。连接码过期后，再次点击应用图标。

## 无桌面主机：配置与自启

在仓库根目录执行：

```bash
python3 linux/campus_linux.py configure
python3 linux/campus_linux.py install --enable --start
```

`configure` 交互设置账号、密码、间隔和区域。`install` 安装服务，`--enable` 开启启动项，`--start` 启动服务；`--desktop` 安装应用入口，`--open` 打开面板。

免登录开机运行需要启用 Linger：

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger
```

输出应为 `Linger=yes`。面板会根据启动项和 Linger 状态显示“已开启”或“仅登录后启动”。

前台测试：

```bash
python3 linux/campus_linux.py run --paused
```

`--paused` 以暂停状态启动，随后可在面板开始保活；前台运行按 Ctrl+C 退出。

## 手动连接与远程访问

桌面用户可直接点击应用图标。手动连接时：

1. 在运行保活的主机上执行 `python3 linux/campus_linux.py token`，获取管理令牌。
2. 本机浏览器打开 `http://127.0.0.1:8765`。
3. 输入管理令牌，点击“连接”。

从另一台电脑访问时，先建立 SSH 隧道：

```bash
ssh -N -L 8765:127.0.0.1:8765 your-user@your-server
```

使用其他 SSH 端口时，加上 `-p 实际端口`。保持隧道运行，再在自己电脑的浏览器打开 `http://127.0.0.1:8765`。本机 8765 已占用时，可改用 `-L 8876:127.0.0.1:8765`，并打开本机 8876 端口。

## 日常命令

以下命令在完整仓库根目录执行：

| 操作 | 命令 |
| --- | --- |
| 打开面板 | `python3 linux/campus_linux.py open` |
| 查看状态 | `python3 linux/campus_linux.py status` |
| 开始保活 | `python3 linux/campus_linux.py start` |
| 立即检查 / 登录 | `python3 linux/campus_linux.py check` |
| 停止保活 | `python3 linux/campus_linux.py stop` |
| 退出后台 | `python3 linux/campus_linux.py quit` |
| 查看开机启动 | `python3 linux/campus_linux.py autostart status` |
| 关闭开机启动 | `python3 linux/campus_linux.py autostart off` |
| 开启开机启动 | `python3 linux/campus_linux.py autostart on` |
| 查询认证状态 | `python3 linux/campus_linux.py probe` |
| 检查官方客户端 | `python3 linux/campus_linux.py portal-info` |

保活暂停状态会保存。修正账号或密码后，点击“开始保活”恢复运行。

其他账号在线时，保活不会暂停，按设置的保活间隔继续查询；不注销或替换该账号。确认没有账号在线后，再使用已保存的账号密码登录。

## 配置与日志

默认位置支持 `XDG_CONFIG_HOME` / `XDG_STATE_HOME` 的绝对路径设置：

```text
~/.config/szu-campus-keepalive/
    settings.json          # 账号、加密密码、区域、间隔
    secret.key             # Fernet 密钥
    ui.token               # 管理令牌
~/.local/state/szu-campus-keepalive/
    instance.lock          # 实例锁
    server.json            # 管理端口和进程号
    run_state.json         # 保活运行状态
    keepalive.log          # 轮转日志
    last_launcher_error.txt # 最近一次桌面启动错误
~/.config/systemd/user/szu-campus-keepalive.service
~/.local/share/applications/szu-campus-keepalive.desktop
```

应用菜单目录遵循 `XDG_DATA_HOME`；桌面位置通过 `xdg-user-dir DESKTOP` 获取。快捷方式由安装命令生成，并记录本机解释器和项目路径。

## 更新、排错与卸载

更新源码、移动仓库或切换 Python 环境后，重新安装并重启服务：

```bash
python3 linux/campus_linux.py install --desktop
systemctl --user restart szu-campus-keepalive.service
python3 linux/campus_linux.py open
systemctl --user status szu-campus-keepalive.service --no-pager
journalctl --user -u szu-campus-keepalive.service -n 40 --no-pager
```

无桌面主机省略 `--desktop` 和 `open`；自定义端口需附上原来的 `--port` 参数。配置解密失败时，恢复与 `settings.json` 配对的 `secret.key`，并检查文件权限。

卸载先取消自启并停止服务：

```bash
systemctl --user disable --now szu-campus-keepalive.service
```

然后删除本项目的服务文件、应用入口、桌面快捷方式、配置与状态目录、`~/.local/lib/szu-campus-keepalive/` 和项目源码，最后执行：

```bash
systemctl --user daemon-reload
```

## 测试与跨平台维护

```bash
python3 -B -m unittest discover -s linux/tests -v
```

测试覆盖官方客户端交互、在线状态核验、公共协议、配置存储、调度、Web 接口、桌面入口和界面文案。测试使用临时目录、模拟认证和测试进程；Linux 专属测试在 Windows 上跳过，原生快捷方式解析测试需要 PyGObject / GIO。

两个平台使用同一仓库、同一主分支。公共模块位于 `outputs/campus_core.py`，Linux 适配位于 `linux/`。GitHub Actions 在 Windows / Ubuntu、Python 3.10 / 3.12 上运行回归测试。
