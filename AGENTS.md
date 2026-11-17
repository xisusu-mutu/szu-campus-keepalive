# Verification

| 规则 | 由什么拦住 |
| --- | --- |
| Windows 启动变更需在隔离环境验证普通 Python、无 `py` / `pythonw`、错误的 Python 环境变量、无控制台及启动器退出后继续运行；交付附真实界面截图 | `python -B -m unittest discover -s tests -p test_windows_launchers.py -v`（测试解压后的 Windows 包）+ Windows CI + `tests/artifacts/` 实测截图 |

启动器测试只操作临时目录和独立注册表测试项。界面验证使用空配置，避免登录校园网或修改真实开机启动项。
