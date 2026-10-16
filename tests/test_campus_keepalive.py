"""Offline regression tests: no authentication, logout, or startup changes."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, MagicMock, patch
import types
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import campus_core as core

class ParsingTests(unittest.TestCase):
    def test_jsonp_not_javascript(self):
        self.assertEqual(core.parse_jsonp(' cb_1({"error":"ok"}); '), {'error': 'ok'})
        with self.assertRaises(core.PortalError):
            core.parse_jsonp('cb({}); alert(1)')

    def test_current_config_not_old_success_link(self):
        page = '''var CONFIG = {page:'account', acid:"12", ip:"172.31.137.43", nas:""};'''
        self.assertEqual(core.parse_teaching_config(page), {'ac_id':'12','ip':'172.31.137.43','nas':''})
        with self.assertRaises(core.PortalError):
            core.parse_teaching_config('<a href="/srun_portal_success?ac_id=2">成功</a>')

    def test_success_requires_online_account_and_ip(self):
        self.assertTrue(core.parse_status('teaching', {'error':'ok','user_name':'test','online_ip':'172.31.1.2'}).online)
        self.assertFalse(core.parse_status('teaching', {'error':'ok','suc_msg':'login_ok'}).online)
        self.assertFalse(core.parse_status('teaching', {'error':'not_online_error'}).online)
        self.assertFalse(core.parse_status('dorm', {'result':1}).online)
        self.assertTrue(core.parse_status('dorm', {'result':1,'uid':'test','v4ip':'172.31.1.2'}).online)

    def test_interval_validation(self):
        self.assertEqual(core.validate_interval('15'), 15)
        for value in ('nan','inf','-1','0','','1441','abc',True):
            with self.subTest(value=value), self.assertRaises(ValueError): core.validate_interval(value)

    def test_retry_not_fifteen_minutes(self):
        self.assertEqual(core.retry_delay(1,900),30)
        self.assertEqual(core.retry_delay(2,900),60)
        self.assertEqual(core.retry_delay(30,900),300)

    def test_untrusted_url_rejected(self):
        for url in ('https://evil.example/','http://net.szu.edu.cn/','https://net.szu.edu.cn.evil.example/'):
            with self.assertRaises(core.PortalError): core.validate_url(url)
        core.validate_url(core.TEACHING_BASE)

    def test_redaction(self):
        value=core.Redactor(['test-secret']).clean({'msg':'bad test-secret','password':'secret','info':'secret','challenge':'secret'})
        self.assertNotIn('secret',json.dumps(value))

class FakePortal:
    def __init__(self,name,statuses,error=None):
        self.name,self.statuses,self.error=name,list(statuses),error
        self.logins=0
    def status(self):
        value=self.statuses.pop(0) if len(self.statuses)>1 else self.statuses[0]
        if isinstance(value,Exception): raise value
        return value
    def login(self,username,password):
        self.logins+=1
        if self.error: raise self.error
        return {'suc_msg':'login_ok'}

def online(name='teaching',account='test'):
    return core.Status(name,True,account,'172.31.1.2',{'error':'ok'})
def offline(name='teaching'):
    return core.Status(name,False,'','',{'error':'not_online_error'})

class EngineTests(unittest.TestCase):
    def run_engine(self,portals,allow_login=True,mode='auto',stop=None):
        engine=core.Engine(portals=portals,stop=stop or threading.Event(),verification_wait=0)
        return engine.check(core.Settings(username='test',password='test-secret',mode=mode),allow_login=allow_login)
    def test_online_no_repeat_login(self):
        p=FakePortal('teaching',[online()])
        self.assertTrue(self.run_engine([p]).online)
        self.assertEqual(p.logins,0)
    def test_unreachable_first_portal_falls_back(self):
        a=FakePortal('teaching',[core.PortalError('timeout')])
        b=FakePortal('dorm',[offline('dorm'),online('dorm')])
        self.assertEqual(self.run_engine([a,b]).portal,'dorm')
        self.assertEqual(b.logins,1)
    def test_ack_without_online_record_is_not_success(self):
        p=FakePortal('teaching',[offline()])
        self.assertFalse(self.run_engine([p]).online)
        self.assertEqual(p.logins,1)
    def test_check_both_before_login(self):
        a=FakePortal('teaching',[offline()]);b=FakePortal('dorm',[online('dorm')])
        self.assertTrue(self.run_engine([a,b]).online)
        self.assertEqual(a.logins+b.logins,0)
    def test_other_account_not_replaced(self):
        p=FakePortal('teaching',[online(account='someone')])
        self.assertEqual(self.run_engine([p]).state,'other_account')
        self.assertEqual(p.logins,0)
    def test_readonly_never_logs_in(self):
        p=FakePortal('teaching',[offline()]);self.run_engine([p],False)
        self.assertEqual(p.logins,0)
    def test_bad_password_stops_attempts(self):
        a=FakePortal('teaching',[offline()],core.CredentialsError('密码错误'))
        b=FakePortal('dorm',[offline('dorm')])
        self.assertEqual(self.run_engine([a,b]).state,'needs_attention')
        self.assertEqual(b.logins,0)
    def test_route_failure_can_fall_back(self):
        a=FakePortal('teaching',[offline()],core.PortalError('接入点不匹配'))
        b=FakePortal('dorm',[offline('dorm'),online('dorm')])
        self.assertTrue(self.run_engine([a,b]).online)
    def test_stop_prevents_login(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(core.Cancelled): self.run_engine([],stop=stop)

class PersistenceTests(unittest.TestCase):
    def test_password_not_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json';store=core.SettingsStore(path)
            store.save(core.Settings(username='test',password='fake-password',interval_minutes=2.5))
            self.assertNotIn('fake-password',path.read_text(encoding='utf-8'))
            self.assertEqual(store.load().password,'fake-password')
            self.assertEqual(store.load().interval_minutes,2.5)

class ProtocolTests(unittest.TestCase):
    def test_numeric_account_errors_also_pause_instead_of_hammering(self):
        for code in ('E2531','E2533','E2553','E2536','E2606'):
            with self.subTest(code=code), self.assertRaises(core.CredentialsError):
                core.auth_failure({'error':'login_error','ecode':code},core.Redactor())

    def test_xencode_matches_live_school_javascript_ascii(self):
        self.assertEqual(core.srun_encode('test-password','0123456789abcdef0123456789abcdef'),
                         '{SRBX1}nDmE/0mHmQrfhBeNLI948ZLMVRt=')

    def test_xencode_matches_live_school_javascript_unicode(self):
        self.assertEqual(core.srun_encode('密码🔑','0123456789abcdef0123456789abcdef'),
                         '{SRBX1}FK5VYHqZKUZ=')

    def client(self):
        client=Mock()
        client.redactor=core.Redactor(['fake-password'])
        return client

    def test_teaching_auth_uses_live_ip_ac_and_verified_protocol(self):
        client=self.client()
        page='''var CONFIG={acid:"12",ip:"172.31.1.2",nas:""};<script src="/static/themes/pro/js/creater.js"></script>'''
        client.get.side_effect=[(page,core.TEACHING_BASE+'/srun_portal_pc'),('cryptography:false',core.TEACHING_BASE+'/static/themes/pro/js/creater.js')]
        client.json.side_effect=[{'error':'ok','challenge':'0123456789abcdef0123456789abcdef','client_ip':'172.31.1.2'}, {'error':'ok','suc_msg':'login_ok'}]
        portal=core.TeachingPortal(client)
        portal.login('test','fake-password')
        url,params=client.json.call_args.args
        self.assertEqual(url,core.TEACHING_BASE+'/cgi-bin/srun_portal')
        self.assertEqual(params['ac_id'],'12')
        self.assertEqual(params['ip'],'172.31.1.2')
        self.assertTrue(params['info'].startswith('{SRBX1}'))
        self.assertNotEqual(params['password'],'fake-password')
        self.assertTrue(client.json.call_args.kwargs['credentials'])

    def test_dorm_gateway_protocol_is_not_legacy_form_guess(self):
        client=self.client()
        page="v46ip='172.31.1.3';ss4='000000000000';vlanid='0';"
        client.get.side_effect=[(page,core.DORM_BASE+'/a79.htm'),("var jsVersion='4.1.3';",core.DORM_BASE+'/a59.js')]
        client.json.side_effect=[{'data':{'login_method':1,'account_prefix':1,'custom_perceive':0}}, {'result':1}]
        core.DormPortal(client).login('test','fake-password')
        url,params=client.json.call_args.args
        self.assertEqual(url,core.DORM_BASE+':801/eportal/portal/login')
        self.assertEqual(params['user_account'],',0,test')
        self.assertEqual(params['wlan_user_ip'],'172.31.1.3')
        self.assertEqual(params['wlan_ac_ip'],'')
        self.assertEqual(params['jsVersion'],'4.1.3')

    def test_dorm_local_auth_method_zero(self):
        client=self.client()
        client.get.return_value=("v46ip='172.31.1.3';",core.DORM_BASE+'/a79.htm')
        client.json.side_effect=[{'data':{'login_method':0,'en_md5':0}},{'result':1}]
        core.DormPortal(client).login('test','fake-password')
        url,params=client.json.call_args.args
        self.assertEqual(url,core.DORM_BASE+'/drcom/login')
        self.assertEqual(params['DDDDD'],'test')
        self.assertNotIn('user_account',params)

    def test_no_credentials_forwarded_even_for_allowed_redirect(self):
        req=urllib.request.Request(core.DORM_BASE+'/drcom/login')
        req.contains_credentials=True
        with self.assertRaises(core.PortalError):
            core.SafeRedirect().redirect_request(req,None,302,'',{},core.TEACHING_BASE+'/')


class StartupTests(unittest.TestCase):
    def test_only_our_startup_entry_changes_and_state_is_read_back(self):
        values={'UnrelatedApplication':'leave-me-alone'}
        context=MagicMock()
        context.__enter__.return_value='key'
        def query(key,name):
            if name not in values: raise FileNotFoundError
            return values[name],1
        def write(key,name,reserved,kind,value): values[name]=value
        def remove(key,name):
            if name not in values: raise FileNotFoundError
            del values[name]
        registry=types.SimpleNamespace(HKEY_CURRENT_USER=1,KEY_READ=1,KEY_SET_VALUE=2,REG_SZ=1,
            OpenKey=Mock(return_value=context),CreateKeyEx=Mock(return_value=context),
            QueryValueEx=query,SetValueEx=write,DeleteValue=remove)
        with patch.dict(sys.modules,{'winreg':registry}):
            startup=core.AutoStart()
            self.assertEqual(startup.state(),'disabled')
            startup.set_enabled(True)
            self.assertEqual(startup.state(),'enabled')
            self.assertIn('--autostart',values[startup.NAME])
            startup.set_enabled(False)
            self.assertEqual(startup.state(),'disabled')
            self.assertEqual(values,{'UnrelatedApplication':'leave-me-alone'})

    def test_single_instance_lock_released(self):
        first=core.SingleInstance()
        try:
            self.assertFalse(first.already_running)
            second=core.SingleInstance()
            try: self.assertTrue(second.already_running)
            finally: second.close()
        finally: first.close()


class WorkerTests(unittest.TestCase):
    def test_stop_interrupts_fifteen_minute_wait(self):
        received=[]
        def emit(name,value):
            received.append(name)
            if name=='next': worker.stop()
        worker=core.KeepaliveWorker(core.Settings(),emit)
        fake=Mock();fake.check.return_value=core.CheckResult(True,'online','ok')
        with patch.object(core,'Engine',return_value=fake):
            worker.start();worker.thread.join(2)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(fake.check.call_count,1)
        self.assertIn('done',received)

    def test_password_error_pauses_automatic_retries(self):
        received=[]
        worker=core.KeepaliveWorker(core.Settings(),lambda name,value:received.append(name))
        fake=Mock();fake.check.return_value=core.CheckResult(False,'needs_attention','bad password')
        with patch.object(core,'Engine',return_value=fake):
            worker.start();worker.thread.join(2)
        self.assertFalse(worker.thread.is_alive())
        self.assertNotIn('next',received)
        self.assertIn('done',received)

    def test_corrupt_config_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json';path.write_text('{broken',encoding='utf-8')
            with self.assertRaises(ValueError): core.SettingsStore(path).load()
            self.assertEqual(path.read_text(),'{broken')

if __name__=='__main__': unittest.main()
