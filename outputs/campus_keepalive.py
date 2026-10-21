"""Windows desktop interface for the SZU campus keepalive client."""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import webbrowser

import campus_core as core
import campus_tray

MODE_LABELS = {'自动兼容两区': 'auto', '仅教学区': 'teaching', '仅宿舍区': 'dorm'}


class CampusApp:
    def __init__(self, root, autostart=False, initial_probe=True, start_immediately=False):
        self.root = root
        self.autostart = autostart
        self.worker = None
        self.stopping = False
        self.pending_start = False
        self.events = queue.Queue()
        self.next_check = None
        self.last_result = None
        self.closed = False
        self.tray = None
        self.hidden_to_tray = False
        self.hide_notice_shown = False
        self.store = core.SettingsStore()
        self.run_store = core.RunStateStore()
        self.startup = core.AutoStart()
        self.config_error = ''
        self.run_state_error = ''
        try:
            settings = self.store.load()
        except ValueError as exc:
            settings = core.Settings()
            self.config_error = str(exc)
        self.settings = settings
        try:
            self.desired_running = self.run_store.load()
        except ValueError as exc:
            self.desired_running = False
            self.run_state_error = str(exc)
        if start_immediately:
            self.desired_running = True
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger('campus_gui')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = RotatingFileHandler(core.DATA_DIR / 'keepalive.log', maxBytes=150_000,
                                            backupCount=2, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', '%Y-%m-%d %H:%M:%S %z'))
        self.logger.addHandler(self.handler)
        self._build_ui()
        self.root.protocol('WM_DELETE_WINDOW', self.hide_window)
        self.root.report_callback_exception = self.callback_error
        self.create_tray()
        self.refresh_startup()
        launch_text = '随 Windows 登录自动启动。' if autostart else (
            '手动启动；立即开始保活。' if start_immediately else '手动启动；恢复上次的保活开关。')
        self.log('启动方式：' + launch_text)
        if self.config_error:
            self.log(self.config_error)
            self.login_state.set('配置需要重新填写')
        if self.run_state_error:
            self.log(self.run_state_error)
        self.root.after(100, self.poll)
        self.root.after(200, self.tick)
        # The GUI smoke test disables all automatic work with initial_probe=False.
        if initial_probe or autostart or start_immediately:
            self.root.after(300, self.resume_keepalive)

    def _build_ui(self):
        root = self.root
        root.title(core.APP_TITLE)
        # Tk font sizes scale with Windows DPI; fixed pixel dimensions hid the logs.
        scale = max(1.0, root.winfo_fpixels('1i') / 96)
        width = min(round(720 * scale), root.winfo_screenwidth() - 60)
        height = min(round(650 * scale), root.winfo_screenheight() - 100)
        root.geometry(f'{width}x{height}')
        root.minsize(min(width, round(660 * scale)), min(height, round(590 * scale)))
        root.configure(bg='#edf2f7')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#edf2f7', foreground='#182b43')
        style.configure('TButton', padding=(12, 8))
        style.configure('Accent.TButton', background='#245eda', foreground='white')
        style.map('Accent.TButton', background=[('active', '#174fc6'), ('disabled', '#98a7bf')])
        style.configure('Startup.TCheckbutton', font=('Microsoft YaHei UI', 10, 'bold'),
                        background='white', padding=(round(12 * scale), round(10 * scale)),
                        indicatorsize=round(24 * scale), indicatormargin=(0, 0, round(10 * scale), 0),
                        indicatorbackground='white', indicatorforeground='white',
                        upperbordercolor='#64748b', lowerbordercolor='#64748b',
                        focuscolor='#245eda', focusthickness=2)
        style.map('Startup.TCheckbutton',
                  background=[('selected', '#e5edff'), ('active', '#e5edff')],
                  foreground=[('selected', '#174fc6')],
                  indicatorbackground=[('selected', '#245eda')],
                  upperbordercolor=[('selected', '#245eda')],
                  lowerbordercolor=[('selected', '#245eda')])
        style.configure('Card.TFrame', background='white')
        style.configure('Card.TLabel', background='white')
        style.configure('Status.TLabel', background='white', font=('Microsoft YaHei UI', 19, 'bold'))
        style.configure('Small.TLabel', foreground='#53657b', font=('Microsoft YaHei UI', 9))
        style.configure('TEntry', fieldbackground='white', padding=6)
        style.configure('TSpinbox', fieldbackground='white', padding=6)
        style.configure('TCombobox', fieldbackground='white', padding=5)
        outer = ttk.Frame(root, padding=20)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='校园网保活', font=('Microsoft YaHei UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='自动兼容宿舍区与教学区', style='Small.TLabel').pack(anchor='w', pady=(3, 12))

        cards = ttk.Frame(outer)
        cards.pack(fill='x')
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        login_card = ttk.Frame(cards, style='Card.TFrame', padding=14)
        login_card.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        ttk.Label(login_card, text='登录状态', style='Card.TLabel').pack(anchor='w')
        self.login_state = tk.StringVar(value='尚未检查')
        self.status_label = ttk.Label(login_card, textvariable=self.login_state, style='Status.TLabel')
        self.status_label.pack(anchor='w', pady=(5, 3))
        self.account_status = tk.StringVar(value='等待认证接口返回结果')
        ttk.Label(login_card, textvariable=self.account_status, style='Card.TLabel').pack(anchor='w')
        boot_card = ttk.Frame(cards, style='Card.TFrame', padding=14)
        boot_card.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
        ttk.Label(boot_card, text='开机启动（登录 Windows 后）', style='Card.TLabel').pack(anchor='w')
        self.startup_text = tk.StringVar(value='正在检查…')
        ttk.Label(boot_card, textvariable=self.startup_text, style='Status.TLabel').pack(anchor='w', pady=(5, 3))
        ttk.Label(boot_card, text='本次启动：' + ('开机自动启动' if self.autostart else '手动启动'), style='Card.TLabel').pack(anchor='w')

        config = ttk.LabelFrame(outer, text='账号与保活设置', padding=12)
        config.pack(fill='x', pady=12)
        config.columnconfigure(1, weight=1)
        config.columnconfigure(3, weight=1)
        self.username = tk.StringVar(value=self.settings.username)
        self.password = tk.StringVar(value=self.settings.password)
        self.interval = tk.StringVar(value=f'{self.settings.interval_minutes:g}')
        self.mode = tk.StringVar(value=next(k for k, v in MODE_LABELS.items() if v == self.settings.mode))
        ttk.Label(config, text='账号').grid(row=0, column=0, sticky='w', padx=(0, 8))
        ttk.Entry(config, textvariable=self.username).grid(row=0, column=1, sticky='ew', padx=(0, 18))
        ttk.Label(config, text='密码').grid(row=0, column=2, sticky='w', padx=(0, 8))
        ttk.Entry(config, textvariable=self.password, show='●').grid(row=0, column=3, sticky='ew')
        ttk.Label(config, text='保活间隔 / 分钟').grid(row=1, column=0, sticky='w', pady=8, padx=(0, 8))
        ttk.Spinbox(config, from_=.5, to=1440, increment=.5, textvariable=self.interval, width=12).grid(row=1, column=1, sticky='ew', padx=(0, 18))
        ttk.Label(config, text='认证区域').grid(row=1, column=2, sticky='w', padx=(0, 8))
        ttk.Combobox(config, textvariable=self.mode, values=list(MODE_LABELS), state='readonly').grid(row=1, column=3, sticky='ew')
        self.startup_checked = tk.BooleanVar(value=False)
        self.startup_label = tk.StringVar(value='开机自动保活：正在检查…')
        self.startup_check = ttk.Checkbutton(config, textvariable=self.startup_label,
                                            variable=self.startup_checked, command=self.toggle_startup,
                                            style='Startup.TCheckbutton', takefocus=True, cursor='hand2')
        self.startup_check.grid(row=2, column=0, columnspan=3, sticky='w')
        ttk.Button(config, text='保存设置', command=self.save).grid(row=2, column=3, sticky='e')

        buttons = ttk.Frame(outer)
        buttons.pack(fill='x', pady=(0, 9))
        self.start_button = ttk.Button(buttons, text='开始保活', style='Accent.TButton', command=self.start)
        self.start_button.pack(side='left', padx=(0, 8))
        self.check_button = ttk.Button(buttons, text='立即检查 / 登录', command=self.check_now)
        self.check_button.pack(side='left', padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text='停止保活', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        ttk.Button(buttons, text='打开认证网页', command=self.open_portal).pack(side='right')

        self.running_state = tk.StringVar(value='保活：未启动')
        self.clock = tk.StringVar(value='系统时间：—')
        self.check_time = tk.StringVar(value='最近检查：—    下次检查：—')
        times = ttk.Frame(outer)
        times.pack(fill='x')
        ttk.Label(times, textvariable=self.running_state).pack(side='left')
        ttk.Label(times, textvariable=self.clock, style='Small.TLabel').pack(side='right')
        ttk.Label(outer, textvariable=self.check_time, style='Small.TLabel').pack(anchor='w', pady=(4, 8))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill='both', expand=True)
        self.log_text = scrolledtext.ScrolledText(notebook, height=8, wrap='word', font=('Microsoft YaHei UI', 9),
                                                bg='white', fg='#26394f', relief='flat', state='disabled')
        notebook.add(self.log_text, text='  运行日志  ')
        # Separate from the window's close button: this actually terminates the app.
        menu = tk.Menu(root)
        program = tk.Menu(menu, tearoff=False)
        program.add_command(label='隐藏到托盘', command=self.hide_window)
        program.add_separator()
        program.add_command(label='退出程序（保留保活状态）', command=self.close)
        menu.add_cascade(label='程序', menu=program)
        root.configure(menu=menu)

    def create_tray(self):
        try:
            self.tray = campus_tray.TrayIcon(lambda action: self.events.put(('tray', action)))
        except Exception as exc:
            self.tray = None
            self.log(f'托盘暂不可用（{type(exc).__name__}）；窗口将保持可见，可通过“程序”菜单退出。')

    def hide_window(self):
        if self.closed:
            return
        if self.tray is None:
            self.create_tray()
        if self.tray is None or not self.tray.ensure_visible():
            self.show_window()
            messagebox.showwarning('暂时无法隐藏到托盘',
                                   '未能创建托盘图标，窗口会保持可见。\n可先最小化；要停止并退出，请用“程序 → 退出程序”。',
                                   parent=self.root)
            return
        self.root.withdraw()
        self.hidden_to_tray = True
        running = self.worker is not None and not self.worker.once
        status = '定时保活继续运行。' if running else '窗口已隐藏；未开启定时保活。'
        self.log('已隐藏到右下角托盘；' + status)
        if not self.hide_notice_shown:
            self.tray.notify(status + '\n点击蓝色 N 图标恢复窗口；右键可退出程序。')
            self.hide_notice_shown = True

    def show_window(self):
        if self.closed:
            return
        hidden = self.hidden_to_tray
        self.hidden_to_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if hidden:
            self.log('窗口已从托盘恢复。')

    def tray_action(self, action):
        if action == 'show':
            self.show_window()
        elif action == 'start':
            # Surface validation dialogs if the saved credentials/settings need attention.
            self.show_window()
            self.start()
        elif action == 'stop':
            self.stop()
        elif action == 'exit':
            self.close()
        elif action == 'shutdown':
            self.close(force=True)
        elif action == 'unavailable':
            self.show_window()
            self.log('托盘图标暂不可用，已恢复窗口。')

    def log(self, text):
        text = core.Redactor([self.password.get()]).clean(str(text))
        self.logger.info(text)
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f'[{core.system_time()}] {text}\n')
        lines = int(self.log_text.index('end-1c').split('.')[0])
        if lines > 300:
            self.log_text.delete('1.0', f'{lines-300}.0')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def read_form(self):
        value = core.Settings(self.username.get(), self.password.get(),
                              core.validate_interval(self.interval.get()), MODE_LABELS[self.mode.get()])
        value.validate()
        return value

    def save(self, quiet=False):
        try:
            settings = self.read_form()
            self.store.save(settings)
            self.settings = settings
            self.config_error = ''
            if self.worker and not self.worker.once:
                self.worker.update(settings)
            if not quiet:
                self.log(f'设置已保存；正常保活间隔 {settings.interval_minutes:g} 分钟。')
            return True
        except (OSError, ValueError, KeyError) as exc:
            self.log('设置未保存：' + str(exc))
            messagebox.showerror('设置未保存', str(exc), parent=self.root)
            return False

    def remember_running(self, enabled):
        self.desired_running = enabled
        try:
            self.run_store.save(enabled)
            self.run_state_error = ''
        except OSError:
            self.run_state_error = '保活开关未能保存；本次操作已生效，但下次启动可能无法恢复，请检查文件权限和磁盘空间。'
            self.log(self.run_state_error)

    def resume_keepalive(self):
        if self.closed:
            return
        if not self.settings.username or not self.settings.password:
            self.log('尚未保存账号或密码，未自动开启保活；请填写后点击“开始保活”。')
            if not self.config_error:
                self.login_state.set('等待填写账号密码')
            return
        if self.desired_running:
            self.start(automatic=True)
        else:
            self.running_state.set('保活：已停止（可点击开始）')
            self.log('上次保活为关闭状态，本次保持停止。')

    def refresh_startup(self):
        state = self.startup.state()
        self.startup_checked.set(state == 'enabled')
        self.startup_text.set({'enabled': '已开启', 'disabled': '未开启', 'different': '启动项指向其他位置', 'error': '无法读取状态'}[state])
        self.startup_label.set('开机自动保活：' + {
            'enabled': '已开启', 'disabled': '已关闭',
            'different': '需更新启动项', 'error': '无法读取状态',
        }[state])

    def toggle_startup(self):
        enabled = self.startup_checked.get()
        if enabled and not self.save(quiet=True):
            self.refresh_startup()
            return
        if enabled and self.startup.state() == 'different':
            if not messagebox.askyesno('更新启动项', '现有校园网启动项指向其他文件。改为启动本窗口吗？', parent=self.root):
                self.refresh_startup()
                return
        try:
            self.startup.set_enabled(enabled)
            self.log('开机启动已' + ('开启。' if enabled else '关闭。'))
        except OSError as exc:
            messagebox.showerror('开机启动设置失败', str(exc), parent=self.root)
            self.refresh_startup()
            return
        self.refresh_startup()
        if enabled:
            self.remember_running(True)
            self.start(automatic=True)

    def launch_worker(self, once=False, allow_login=True):
        if self.worker:
            return
        self.stopping = False
        self.next_check = None
        self.worker = core.KeepaliveWorker(self.settings, lambda name, data: self.events.put((name, data)), once, allow_login)
        self.running_state.set('正在单次检查（未开启定时保活）' if once else '保活：运行中')
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.worker.start()

    def start(self, automatic=False):
        if self.closed or (self.worker and not self.worker.once and not self.stopping):
            return
        # Automatic launch uses the configuration already loaded or saved by the checkbox.
        if not automatic and not self.save(quiet=True):
            return
        if self.config_error:
            self.log('配置尚未恢复，未自动开启保活；请重新填写并保存账号密码。')
            return
        if not self.settings.username or not self.settings.password:
            self.log('尚未保存账号或密码，未开启保活；请填写后点击“开始保活”。')
            self.login_state.set('等待填写账号密码')
            return
        self.remember_running(True)
        if self.worker:
            self.pending_start = True
            self.log('已开启保活；当前检查结束后自动开始定时保活。')
            return
        self.log(f'开始保活：间隔 {self.settings.interval_minutes:g} 分钟；先立即检查。')
        try:
            self.launch_worker()
        except Exception as exc:
            self.worker = None
            self.remember_running(False)
            self.running_state.set('保活：启动失败（可点击开始重试）')
            self.start_button.configure(state='normal')
            self.stop_button.configure(state='disabled')
            self.log(f'保活任务启动失败（{type(exc).__name__}）。')

    def check_now(self):
        if self.worker:
            if not self.worker.once:
                self.worker.check_now()
                self.log('已请求立即检查。')
            return
        if self.save(quiet=True):
            self.launch_worker(once=True)

    def stop(self):
        self.pending_start = False
        self.remember_running(False)
        if self.worker:
            self.stopping = True
            self.worker.stop()
            self.next_check = None
            self.running_state.set('正在停止（等待当前网络请求结束）…')
            self.stop_button.configure(state='disabled')
            self.log('已发出停止指令。')
        else:
            self.running_state.set('保活：已停止（可点击开始）')
            self.log('保活已关闭，下次启动保持停止。')

    def show_result(self, result):
        self.last_result = result
        state_text = {'online': '已成功登录 / 在线', 'offline': '尚未登录', 'unreachable': '无法连接认证站点',
                      'needs_attention': '需要处理', 'other_account': '其他账号在线'}
        self.login_state.set(state_text.get(result.state, '状态未知'))
        self.status_label.configure(foreground='#16814a' if result.online else '#a95814')
        if result.portal:
            self.account_status.set(f'{core.PORTAL_NAMES[result.portal]} · {result.ip}')
        else:
            self.account_status.set('未取得可验证的在线记录')
        self.log(result.message)
        self.log('本轮操作完成，系统时间：' + result.checked_at)

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'tray':
                    self.tray_action(value)
                    if self.closed:
                        return
                elif event == 'log':
                    self.log(value)
                elif event == 'checking':
                    self.next_check = None
                    self.login_state.set('正在检查…')
                    self.status_label.configure(foreground='#245eda')
                elif event == 'result':
                    self.show_result(value)
                elif event == 'next':
                    if not self.stopping:
                        self.next_check = value
                        self.log('下次检查：' + datetime.fromtimestamp(value[0]).strftime('%Y-%m-%d %H:%M:%S'))
                elif event == 'error':
                    self.login_state.set('检查异常，已停止')
                    self.log(value)
                elif event == 'done':
                    completed = self.worker
                    self.worker = None
                    self.stopping = False
                    self.next_check = None
                    if self.login_state.get() == '正在检查…':
                        self.login_state.set('已停止检查')
                        self.status_label.configure(foreground='#53657b')
                    self.start_button.configure(state='normal')
                    self.stop_button.configure(state='disabled')
                    self.running_state.set('保活：已停止（可点击开始）')
                    if self.pending_start:
                        self.pending_start = False
                        self.start(automatic=True)
                    elif completed and not completed.once:
                        self.remember_running(False)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def tick(self):
        if self.closed:
            return
        self.clock.set('系统时间：' + core.system_time())
        previous = self.last_result.checked_at if self.last_result else '—'
        upcoming = '—'
        if self.next_check:
            seconds_left = self.next_check[1] - time.monotonic()
            remaining = max(0, math.ceil(seconds_left))
            upcoming = datetime.fromtimestamp(self.next_check[0]).strftime('%H:%M:%S') + f'（约 {remaining//60:02d}:{remaining%60:02d} 后）'
            if seconds_left <= 0 and self.worker and not self.worker.once:
                self.worker.check_now()  # Also catches overdue checks after Windows resumes.
        self.check_time.set(f'最近检查：{previous}    下次检查：{upcoming}')
        self.refresh_startup()
        if self.tray and not self.tray.update_tooltip(
                f'校园网保活 · {self.running_state.get()}\n{self.login_state.get()}'):
            if self.hidden_to_tray:
                self.tray_action('unavailable')
        self.root.after(1000, self.tick)

    def open_portal(self):
        mode = MODE_LABELS[self.mode.get()]
        if mode == 'auto':
            mode = self.last_result.portal if self.last_result and self.last_result.portal else 'teaching'
        webbrowser.open(core.DORM_BASE + '/a79.htm' if mode == 'dorm' else core.TEACHING_BASE + '/')

    def callback_error(self, exc_type, exc_value, traceback):
        self.show_window()
        self.log(f'界面操作失败（{exc_type.__name__}）；请检查设置后重试。')
        messagebox.showerror('操作失败', f'发生 {exc_type.__name__}，请检查日志后重试。', parent=self.root)

    def close(self, force=False):
        if self.closed:
            return
        # Shutting down the process must not act like clicking "Stop keepalive".
        # Start/stop persist the intended switch immediately, including during shutdown.
        if self.worker:
            self.worker.stop()
        self.closed = True
        if self.run_state_error:
            self.log('程序退出；' + self.run_state_error)
        else:
            self.log('程序退出；保活开关保持' + ('开启，下次启动自动恢复。' if self.desired_running
                                                else '关闭，下次启动保持停止。'))
        if self.tray:
            self.tray.close()
        self.logger.removeHandler(self.handler)
        self.handler.close()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=core.APP_TITLE)
    parser.add_argument('--autostart', action='store_true', help='Windows 登录启动项使用')
    parser.add_argument('--start', action='store_true', help='手动打开并立即开始保活，不修改开机启动项')
    parser.add_argument('--status', action='store_true', help='只检查在线状态，不登录')
    parser.add_argument('--once', action='store_true', help='命令行单次检查，必要时登录')
    parser.add_argument('--smoke-gui', action='store_true', help='界面自检：不联网，1.5 秒后关闭')
    args = parser.parse_args()
    if args.status or args.once:
        lock = core.SingleInstance() if args.once else None
        try:
            if lock and lock.already_running:
                print('已有保活窗口在运行，请在窗口中点击“立即检查 / 登录”。')
                return 2
            settings = core.SettingsStore().load()
            engine = core.Engine(notify=lambda text: print(core.system_time(), text))
            result = engine.check(settings, allow_login=args.once)
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return 0 if result.online else 1
        finally:
            if lock:
                lock.close()
    instance = core.SingleInstance()
    try:
        if instance.already_running:
            if not args.autostart:
                if campus_tray.restore_existing_window():
                    return 0
                # Fallback for a visible/minimized app whose tray could not be created.
                user32 = ctypes.WinDLL('user32', use_last_error=True)
                user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
                user32.FindWindowW.restype = ctypes.c_void_p
                window = user32.FindWindowW(None, core.APP_TITLE)
                if window:
                    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
                    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                    user32.ShowWindow(window, 9)
                    user32.SetForegroundWindow(window)
            return 0
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
        root = tk.Tk()
        app = CampusApp(root, args.autostart and not args.smoke_gui,
                        initial_probe=not args.smoke_gui,
                        start_immediately=args.start and not args.smoke_gui)
        if args.smoke_gui:
            root.after(1500, lambda: app.close(force=True))
        root.mainloop()
        return 0
    finally:
        instance.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # pythonw has no console: ensure launch errors are visible.
        text = f'启动失败（{type(error).__name__}）。请用“排错启动.bat”查看错误，或检查 data/settings.json。'
        if sys.stderr:
            import traceback
            traceback.print_exc()
        else:
            ctypes.windll.user32.MessageBoxW(None, text, core.APP_TITLE, 16)
        raise SystemExit(1)
