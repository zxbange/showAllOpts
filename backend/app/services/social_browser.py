"""Short-lived remote login windows; profiles remain on the application server."""
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit
import uuid
from contextlib import contextmanager

import portalocker

from app.core.config import PROJECT_ROOT


PLATFORMS = {'weibo': 'https://weibo.com/login.php', 'xueqiu': 'https://www.xueqiu.com/'}
HOSTS = {
    'weibo': ('weibo.com', 'weibo.cn', 'sina.com.cn', 'sina.cn', 'sinaimg.cn', 'sinajs.cn'),
    'xueqiu': ('xueqiu.com', 'imedao.com', 'qq.com', 'qpic.cn', 'gtimg.com', 'geetest.com'),
}
ACTIVE = {'starting', 'waiting', 'verification'}
LIFETIME = 360
WIDTH, HEIGHT = 1100, 800
logger = logging.getLogger(__name__)


class LoginRestricted(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__('Login identity verification was restricted')


def browser_error(error):
    """Classify locally; never return exception text containing URLs or credentials."""
    text = str(error).lower()
    if isinstance(error, ModuleNotFoundError) and (getattr(error, 'name', None) or '').startswith('playwright'):
        return 'python_dependency_missing', '服务虚拟环境未安装 Playwright，请使用服务的 Python 安装 backend/requirements.lock.txt'
    if ('executable doesn\'t exist' in text or 'executable not found' in text
            or ('distribution' in text and 'not found' in text)):
        return 'browser_missing', '未找到 Chromium，请用服务对应的虚拟环境运行 scripts/install_social_browser.py，并核对浏览器安装目录'
    if any(word in text for word in ('missing x server', 'missing xserver', 'without having a xserver', '$display')):
        return 'display_unavailable', '已选择显示窗口模式，但服务没有可用显示环境；命令行 Linux 需配置 Xvfb，或改回 SOCIAL_BROWSER_HEADLESS=true'
    if any(word in text for word in ('no usable sandbox', 'operation not permitted', 'failed to move to new namespace', 'running as root without --no-sandbox')):
        return 'sandbox_denied', 'Chromium 沙箱启动被系统拒绝，请检查服务运行账号、用户命名空间及 systemd/AppArmor/容器限制'
    if any(word in text for word in ('error while loading shared libraries', 'host system is missing dependencies', 'cannot open shared object file')):
        return 'system_dependencies_missing', 'Chromium 缺少系统库，请运行虚拟环境中的 python -m playwright install-deps chromium'
    if isinstance(error, PermissionError) or any(word in text for word in ('permission denied', 'eacces', 'read-only file system')):
        return 'permission_denied', '服务账号无法读取浏览器或写入会话目录，请检查 .runtime/playwright 和 LOG_DIR 的权限及 systemd 路径限制'
    if any(word in text for word in ('processsingleton', 'singletonlock', 'profile appears to be in use')):
        return 'profile_busy', '浏览器资料目录被其他进程占用，请检查是否存在残留浏览器；不要在浏览器运行时删除资料目录'
    if 'net::err_' in text:
        return 'network_failed', '服务器访问官方登录页失败，请检查 DNS、代理、TLS 和出口网络'
    if type(error).__name__ == 'TimeoutError':
        return 'timeout', '官方登录页加载或操作超时，请检查服务器网络后重试'
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return 'invalid_response', '登录验证未得到预期数据，可能为平台验证页或接口变化，请检查官方登录画面'
    return 'browser_failed', '浏览器进程或登录操作失败，请运行 scripts/check_social_browser.py 检查启动环境；若检查通过，请反馈本页失败阶段'


def allowed_url(platform, url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and parsed.port in (None, 443) and any(
        host == suffix or host.endswith('.' + suffix) for suffix in HOSTS[platform])


def authenticated_xueqiu(cookies, user):
    """Guest cookies alone must never be accepted as a logged-in account."""
    values = {item['name']: item['value'] for item in cookies}
    return (bool(values.get('xq_a_token') and values.get('xq_r_token'))
            and str(values.get('u', '')).isdigit() and isinstance(user, dict)
            and str(user.get('id', '')) == values['u'] and bool(user.get('screen_name')))


class BrowserLogin:
    def __init__(self, settings, monitor):
        self.settings, self.monitor = settings, monitor
        self.root = monitor.root / 'browser-login'
        self.profiles = monitor.root / 'browser-profiles'
        for path in (self.root, self.profiles):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != 'nt':
                path.chmod(0o700)
        self.threads = []
        self.stopping = threading.Event()

    @property
    def available(self):
        return len(self.settings.social_browser_login_key.get_secret_value()) >= 24

    def path(self, platform, kind):
        if platform not in PLATFORMS:
            raise ValueError('不支持的平台')
        return self.root / f'{platform}.{kind}'

    def write(self, path, data):
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as file:
                file.write(self.monitor.cipher.encrypt(data))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, path):
        try:
            return self.monitor.cipher.decrypt(path.read_bytes())
        except FileNotFoundError:
            return None

    def raw_status(self, platform):
        data = self.read(self.path(platform, 'state'))
        return json.loads(data) if data else {'state': 'idle', 'message': '尚未打开登录窗口'}

    def status(self, platform):
        result = self.raw_status(platform)
        if result['state'] in ACTIVE and (result['expires_at'] <= time.time()
                                         or result.get('updated_at', 0) < time.time() - 70):
            return dict(result, state='expired', message='登录窗口已超时或服务已重启，请重新打开', image=False)
        return result

    def active(self, platform):
        return self.status(platform)['state'] in ACTIVE

    def save(self, platform, record, **changes):
        record.update(changes, updated_at=time.time())
        self.write(self.path(platform, 'state'), json.dumps(record, ensure_ascii=False).encode())

    def start(self, platform):
        if not self.available:
            raise ValueError('请先在服务器私有配置中设置至少 24 位的 SOCIAL_BROWSER_LOGIN_KEY')
        lease = portalocker.Lock(str(self.path(platform, 'lock')), timeout=0)
        try:
            lease.acquire()
        except portalocker.exceptions.LockException:
            raise ValueError('该平台已有登录窗口，请先完成或取消') from None
        state = {'id': uuid.uuid4().hex, 'state': 'starting', 'message': '正在打开官方登录页',
                 'expires_at': time.time() + LIFETIME, 'image': False, 'revision': 0}
        try:
            self.path(platform, 'command').unlink(missing_ok=True)
            self.path(platform, 'image').unlink(missing_ok=True)
            self.save(platform, state)
            thread = threading.Thread(target=self.run, args=(platform, state, lease), daemon=True)
            self.threads = [item for item in self.threads if item.is_alive()]
            self.threads.append(thread)
            thread.start()
        except BaseException:
            lease.release()
            raise
        return state

    def action(self, platform, job_id, action, x=None, y=None):
        with portalocker.Lock(str(self.path(platform, 'commands.lock')), timeout=2):
            state = self.status(platform)
            if state.get('id') != job_id or state['state'] not in ACTIVE:
                raise ValueError('登录窗口已结束，请重新打开')
            if action == 'click' and not (x is not None and y is not None and 0 <= x < WIDTH and 0 <= y < HEIGHT):
                raise ValueError('点击位置超出登录画面')
            # A single outstanding command avoids an unbounded remote interaction queue.
            if self.path(platform, 'command').exists() and action != 'cancel':
                raise ValueError('上一操作仍在处理中，请稍后再试')
            self.write(self.path(platform, 'command'), json.dumps({
                'id': job_id, 'action': action, 'x': x, 'y': y}).encode())
        return state

    def command(self, platform):
        with portalocker.Lock(str(self.path(platform, 'commands.lock')), timeout=2):
            data = self.read(self.path(platform, 'command'))
            self.path(platform, 'command').unlink(missing_ok=True)
        return json.loads(data) if data else None

    def image(self, platform, job_id):
        state = self.status(platform)
        if state.get('id') != job_id or state['state'] not in ACTIVE or not state.get('image'):
            return None
        return self.read(self.path(platform, 'image'))

    def verify(self, platform, context, page):
        origin = 'https://m.weibo.cn/' if platform == 'weibo' else 'https://www.xueqiu.com/'
        cookies = context.cookies([origin])
        if platform == 'weibo':
            if not any(item['name'] == 'SUB' for item in cookies):
                return None
            response = context.request.get('https://m.weibo.cn/api/config', timeout=12000, max_redirects=0)
            if response.status != 200:
                return None
            data = response.json().get('data', {})
            if not data.get('login') or not data.get('uid'):
                return None
        else:
            values = {item['name']: item['value'] for item in cookies}
            if not (values.get('xq_a_token') and values.get('xq_r_token')):
                return None
            if urlsplit(page.url).hostname not in ('xueqiu.com', 'www.xueqiu.com'):
                return None
            # Use the same browser session/network stack as the site's login component.
            result = page.evaluate('''async () => {
                try {
                    const response = await fetch('https://www.xueqiu.com/user/show.json', {
                        credentials: 'include', redirect: 'error',
                        headers: {'X-Requested-With': 'XMLHttpRequest'},
                        signal: AbortSignal.timeout(12000),
                    });
                    const data = response.ok ? await response.json() : null;
                    return {status: response.status, user: data && {
                        id: data.id, screen_name: data.screen_name,
                    }};
                } catch { return {status: 0}; }
            }''')
            if result.get('status') in (401, 403, 418, 429, 567):
                raise LoginRestricted(result['status'])
            user = result.get('user') if result.get('status') == 200 else None
            if not authenticated_xueqiu(cookies, user):
                return None
        # Re-read after the verification request, which can rotate session cookies.
        return '; '.join(f"{item['name']}={item['value']}" for item in context.cookies([origin]))

    @contextmanager
    def browser_context(self, playwright, platform):
        profile = self.profiles / platform
        profile.mkdir(mode=0o700, exist_ok=True)
        options = {'headless': self.settings.social_browser_headless, 'chromium_sandbox': os.name != 'nt',
                   'viewport': {'width': WIDTH, 'height': HEIGHT},
                   'accept_downloads': False, 'service_workers': 'block'}
        if self.settings.social_browser_channel:
            options['channel'] = self.settings.social_browser_channel
        context = playwright.chromium.launch_persistent_context(str(profile), **options)
        try:
            snapshot = self.read(profile / 'cookies.enc')
            if snapshot:
                # Chromium discards session-only cookies on a clean exit. Restore their
                # original expiry attributes explicitly; the platform still controls validity.
                context.add_cookies(json.loads(snapshot))
            yield context
        finally:
            try:
                self.write(profile / 'cookies.enc', json.dumps(context.cookies()).encode())
            finally:
                context.close()

    def run(self, platform, state, lease):
        stage = '启动浏览器'
        try:
            from playwright.sync_api import sync_playwright
            os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(PROJECT_ROOT / '.runtime' / 'playwright'))
            with sync_playwright() as playwright, self.browser_context(playwright, platform) as context:
                context.route('**/*', lambda route: route.continue_() if allowed_url(platform, route.request.url)
                              else route.abort())
                context.route_web_socket('**/*', lambda socket: socket.close())
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(10000)
                page.on('dialog', lambda dialog: dialog.dismiss())
                stage = '打开官方登录页'
                response = page.goto(PLATFORMS[platform], wait_until='domcontentloaded', timeout=45000)
                if response and response.status in (401, 403, 418, 429, 567):
                    self.save(platform, state, state='blocked', image=False,
                              http_status=response.status, error_code='login_page_restricted',
                              message=f'平台登录页返回 HTTP {response.status}，当前浏览器会话访问受限，已停止重试；这不表示已确认服务器 IP 被封')
                    return
                # Select only a visible official login entry; all other interaction is manual.
                if platform == 'xueqiu':
                    entry = page.get_by_text(re.compile(r'^\s*登录\s*(?:[/／]\s*注册)?\s*$')).filter(visible=True)
                    # The header is hydrated asynchronously; do not select a hidden duplicate.
                    try:
                        entry.first.wait_for(state='visible', timeout=15000)
                        entry.first.click()
                    except Exception as error:
                        if type(error).__name__ != 'TimeoutError':
                            raise
                last_probe = 0
                opened_at = time.time()
                while time.time() < state['expires_at'] and not self.stopping.is_set():
                    pages = [item for item in context.pages if not item.is_closed()]
                    if pages:
                        page = pages[-1]
                    command = self.command(platform)
                    stage = '处理登录画面'
                    if command and command['id'] == state['id']:
                        if command['action'] == 'cancel':
                            self.save(platform, state, state='cancelled', message='登录已取消', image=False)
                            return
                        if command['action'] == 'reload':
                            page.goto(PLATFORMS[platform], wait_until='domcontentloaded', timeout=45000)
                            last_probe = 0
                        elif command['action'] == 'click':
                            page.mouse.click(command['x'], command['y'])
                        elif command['action'] == 'check':
                            last_probe = 0
                    text = page.locator('body').inner_text(timeout=10000)
                    if (platform == 'weibo' and '/visitor/visitor' in urlsplit(page.url).path
                            and time.time() - opened_at > 25):
                        self.save(platform, state, state='blocked', image=False,
                                  message='微博停留在访客验证页，服务器未能进入登录页。请检查服务器网络或在平台处理后重试')
                        return
                    if any(word in text for word in ('安全策略拦截', '访问被拒绝', 'Access Denied')):
                        self.save(platform, state, state='blocked', message='平台拒绝服务器访问。请检查服务器网络或联系平台处理，程序已停止重试', image=False)
                        return
                    if time.time() - last_probe >= 10:
                        last_probe = time.time()
                        stage = '验证平台登录状态'
                        credential = self.verify(platform, context, page)
                        if credential:
                            # Serialize completion against a concurrent cancellation request.
                            with portalocker.Lock(str(self.path(platform, 'commands.lock')), timeout=2):
                                pending = self.read(self.path(platform, 'command'))
                                cancelled = pending and json.loads(pending).get('action') == 'cancel'
                                if not cancelled and not self.stopping.is_set() and time.time() < state['expires_at']:
                                    self.monitor.update_config({platform + '_cookie': credential})
                                    self.save(platform, state, state='success', image=False,
                                              message='登录已验证，凭据已加密保存。若平台此前已暂停，请点击平台的“恢复监控”')
                                    return
                            continue
                    verification = any(word in text for word in ('请输入验证码', '请完成安全验证', '拖动滑块'))
                    stage = '保存登录画面'
                    self.write(self.path(platform, 'image'), page.screenshot(type='png'))
                    self.save(platform, state, state='verification' if verification else 'waiting', image=True,
                              revision=state['revision'] + 1,
                              message='平台要求人工验证，请查看画面；若无法操作，请取消并在平台完成验证后重试' if verification
                              else '请选择官方扫码入口，用对应应用扫描并确认；可点击下方画面切换登录方式')
                    self.stopping.wait(2)
                self.save(platform, state, state='expired', image=False, message='登录窗口已结束，请重新打开')
        except Exception as error:
            if isinstance(error, LoginRestricted):
                self.save(platform, state, state='blocked', image=False, error_code='login_verification_restricted',
                          http_status=error.status, stage=stage,
                          message=f'登录身份校验返回 HTTP {error.status}，已停止重试，请在官方浏览器检查账号状态')
                return
            # Exception messages/URLs can contain credentials; expose only a safe classification.
            code, reason = browser_error(error)
            logger.warning('Browser login failed platform=%s stage=%s code=%s', platform, stage, code)
            self.save(platform, state, state='error', image=False, error_code=code, stage=stage,
                      message=f'{stage}：{reason}（{code}）')
        finally:
            # sync_playwright closes the driver/context even on early returns or failures.
            self.path(platform, 'image').unlink(missing_ok=True)
            lease.release()

    def stop(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=50)
