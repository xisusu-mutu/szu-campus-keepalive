"use strict";
const byId = id => document.getElementById(id);
const states = {checking:"正在检查",waiting:"后台保活中",stopping:"正在停止",stopped:"已停止 / 暂停"};
const authStates = {online:"已确认登录",offline:"未确认在线",unreachable:"认证入口不可达",needs_attention:"需要处理账号",other_account:"其他账号在线",error:"检查异常"};
function savedToken(value) {
  try {
    if(value===undefined)return sessionStorage.getItem("campus-management-token") || "";
    if(value)sessionStorage.setItem("campus-management-token",value);
    else sessionStorage.removeItem("campus-management-token");
  } catch (_) { /* A blocked storage policy must not prevent this tab connecting. */ }
  return "";
}
let token = savedToken();
let timer = null, hydrated = false, connected = false, commandBusy = false, polling = false, connecting = false;

function notice(text, error=false) {
  byId("notice").textContent = text;
  byId("notice").className = error ? "error" : "";
}
function disconnected(message="面板已断开。") {
  clearTimeout(timer); connected = false; token = ""; hydrated = false;
  savedToken("");
  byId("token").value = "";
  byId("login-panel").hidden = false;
  byId("dashboard").hidden = true;
  byId("connection").textContent = "面板未连接";
  byId("connection").className = "badge";
  notice(message);
}
async function request(path, data) {
  const options = {headers:{Authorization:"Bearer " + token},cache:"no-store",credentials:"omit"};
  if (data !== undefined) { options.method="POST"; options.headers["Content-Type"]="application/json"; options.body=JSON.stringify(data); }
  const abort = new AbortController(); options.signal=abort.signal;
  const deadline = setTimeout(()=>abort.abort(),40000);
  try {
    const response = await fetch(path,options);
    const value = await response.json();
    if (!response.ok) {
      if (response.status===401) disconnected("管理令牌无效或已更换，请重新连接。");
      throw new Error(value.error || "请求失败。");
    }
    return value;
  } catch(error) {
    if (error.name==="AbortError") throw new Error("管理请求超时，请检查服务状态。");
    if (error instanceof TypeError) throw new Error("无法连接后台，请重新打开应用或检查 SSH 隧道和服务。");
    throw error;
  } finally { clearTimeout(deadline); }
}
function render(s) {
  byId("login-panel").hidden=true; byId("dashboard").hidden=false;
  byId("connection").textContent="面板已连接"; byId("connection").className="badge good";
  byId("system-time").textContent="运行主机系统时间："+s.system_time;
  byId("run-status").textContent=states[s.run_state] || s.run_state;
  byId("next-check").textContent=s.next_check ? "下次："+s.next_check+"（约 "+s.remaining_seconds+" 秒）" : "当前没有下一轮计时。";
  byId("launch").textContent=(s.launched_by==="systemd" ? "本次由 systemd 用户服务启动。" : "本次由命令行手动启动。")+" 启动于 "+s.started_at;
  const result=s.result;
  byId("login-status").textContent=result ? (authStates[result.state] || result.state) : "尚未检查";
  byId("login-status").className=result && result.online ? "good-text" : "warn-text";
  byId("login-message").textContent=result ? result.message : (s.config_error || "请保存设置并开始保活。");
  byId("identity").textContent=result ? [result.portal==="teaching"?"教学区":result.portal==="dorm"?"宿舍区":"",result.account||"",result.ip||""].filter(Boolean).join(" · ") : "";
  const a=s.autostart;
  byId("autostart").checked=a.enabled;
  byId("autostart").disabled=commandBusy || !a.installed || Boolean(a.error);
  byId("boot-status").textContent=a.error ? "无法读取" : a.boot_enabled ? "已开启" : a.enabled ? "仅登录后启动" : "已关闭";
  byId("boot-status").className=a.boot_enabled?"good-text":"";
  byId("boot-detail").textContent=a.error || (!a.installed?"请先安装用户服务：install --desktop --open。":a.enabled && !a.linger?"开机运行需启用 Linger：loginctl enable-linger。":"");
  byId("saved-config").textContent="当前："+s.settings.interval_minutes+" 分钟 / "+s.settings.mode;
  if (!hydrated) {
    byId("username").value=s.settings.username; byId("mode").value=s.settings.mode;
    byId("interval").value=s.settings.interval_minutes; hydrated=true;
  }
  byId("start").disabled=commandBusy || s.run_state!=="stopped";
  byId("stop").disabled=commandBusy || s.run_state==="stopped" || s.run_state==="stopping";
  byId("check").disabled=commandBusy || s.run_state==="stopping";
  byId("exit").disabled=commandBusy;
  const logs=byId("logs"), bottom=logs.scrollTop+logs.clientHeight>=logs.scrollHeight-35;
  logs.textContent=s.logs.map(item=>item.time+"  "+item.message).join("\n");
  if(bottom) logs.scrollTop=logs.scrollHeight;
}
async function poll() {
  clearTimeout(timer);
  if(!connected || polling)return;
  polling=true;
  const pollToken=token;
  try { const s=await request("/api/state");if(connected && token===pollToken)render(s); }
  catch(error) { if(connected && token===pollToken){byId("connection").textContent="面板连接中断";byId("connection").className="badge";notice(error.message,true);} }
  finally { polling=false;if(connected)timer=setTimeout(poll,3000); }
}
async function action(path,data={},message="操作已提交。") {
  if(commandBusy)return;
  commandBusy=true;
  try { await request(path,data); notice(message); await poll(); }
  catch(error) { notice(error.message,true); }
  finally { commandBusy=false; if(connected) await poll(); }
}
byId("connect-form").addEventListener("submit",async event=>{
  event.preventDefault();
  if(connecting)return;
  connecting=true; token=byId("token").value.trim();
  try { await connectPanel(); }
  catch(error) { notice(error.message,true); }
  finally { connecting=false; }
});
byId("settings-form").addEventListener("submit",async event=>{
  event.preventDefault();
  if(commandBusy)return;
  const data={username:byId("username").value,password:byId("password").value,mode:byId("mode").value,interval_minutes:Number(byId("interval").value)};
  commandBusy=true;
  try { await request("/api/settings",data);byId("password").value="";hydrated=false;notice("设置已保存并应用。"); }
  catch(error) { notice(error.message,true); }
  finally {commandBusy=false;await poll();}
});
byId("start").addEventListener("click",()=>action("/api/start"));
byId("stop").addEventListener("click",()=>action("/api/stop",{},"保活正在停止。"));
byId("check").addEventListener("click",()=>action("/api/check",{},"已请求检查。"));
byId("autostart").addEventListener("change",()=>action("/api/autostart",{enabled:byId("autostart").checked},"启动项已更新。"));
byId("disconnect").addEventListener("click",()=>disconnected());
byId("exit").addEventListener("click",async()=>{
  if(commandBusy || !confirm("确定停止保活并退出后台程序吗？"))return;
  commandBusy=true;
  try { await request("/api/exit",{});disconnected("后台已收到退出请求。"); }
  catch(error) {notice(error.message,true);}
  finally {commandBusy=false;}
});
async function connectPanel(message="") {
  const s=await request("/api/state");
  savedToken(token); byId("token").value=""; connected=true;
  notice(message); render(s); clearTimeout(timer); timer=setTimeout(poll,3000);
}
async function bootstrap() {
  const fragment=new URLSearchParams(window.location.hash.slice(1));
  const code=fragment.get("launch");
  // Remove the one-time code before making requests; never put it in a query,
  // localStorage, displayed text, log, referrer, or bookmarked URL.
  if(window.location.hash)history.replaceState(null,"",window.location.pathname+window.location.search);
  if(connecting)return;
  if(code!==null) {
    connecting=true; disconnected("正在自动连接本机管理面板…");
    try {
      if(!/^[A-Za-z0-9_-]{43}$/.test(code))throw new Error("自动连接凭证无效，请再次点击应用图标。");
      const value=await request("/api/ui-connect",{code});
      if(typeof value.token!=="string" || !value.token)throw new Error("后台未返回有效的连接凭证。");
      token=value.token;
      await connectPanel();
    } catch(error) { disconnected(error.message); notice(error.message,true); }
    finally { connecting=false; }
  } else if(token) { connected=true; await poll(); }
}
window.addEventListener("hashchange",bootstrap);
bootstrap();
