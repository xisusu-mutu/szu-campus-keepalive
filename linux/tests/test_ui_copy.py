"""Checks for the compact management panel."""
from pathlib import Path
import unittest


WEB = Path(__file__).resolve().parents[1] / 'web'


class PanelCopyTests(unittest.TestCase):
    def test_panel_keeps_status_and_controls(self):
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        for element in ('login-status', 'run-status', 'system-time', 'autostart',
                        'start', 'check', 'stop', 'exit', 'settings-form',
                        'interval', 'logs', 'notice'):
            with self.subTest(element=element):
                self.assertIn(f'id="{element}"', html)

    def test_removed_explanatory_rows_have_no_render_targets(self):
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        script = (WEB / 'app.js').read_text(encoding='utf-8')
        for element in ('last-check', 'auth-backend', 'password-hint', 'response'):
            self.assertNotIn(f'id="{element}"', html)
            self.assertNotIn(f'byId("{element}")', script)

    def test_panel_omits_disclaimer_copy(self):
        copy = '\n'.join((WEB / name).read_text(encoding='utf-8')
                         for name in ('index.html', 'app.js'))
        for phrase in ('只读预览', '关闭网页不会', '后台保活不受影响',
                       '并非持续实时检测', '不是全网抓包', '无法防御',
                       '不会回传浏览器', '本次保活状态不变', '密码已保存',
                       '尚未保存密码', '最近认证响应', '暂无认证响应'):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, copy)

    def test_settings_save_button_is_kept(self):
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<button type="submit">保存并应用</button>', html)

    def test_automatic_connection_has_no_success_banner(self):
        script = (WEB / 'app.js').read_text(encoding='utf-8')
        bootstrap = script.split('async function bootstrap()', 1)[1]
        self.assertIn('await connectPanel();', bootstrap)
        self.assertNotIn('await connectPanel("', bootstrap)
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        self.assertIn('#notice:empty{display:none}', css)

    def test_log_timestamps_are_kept(self):
        script = (WEB / 'app.js').read_text(encoding='utf-8')
        self.assertIn('item.time+"  "+item.message', script)
        self.assertIn('s.system_time', script)


if __name__ == '__main__':
    unittest.main()
