"""
ClawCloud 自动登录脚本
- 自动检测区域跳转（如 ap-southeast-1.console.claw.cloud）
- 等待设备验证批准（30秒）
- 每次登录后自动更新 Cookie
- Telegram 通知
"""

import base64
import os
import random
import re
import sys
import time
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

# Vaultwarden 集成
try:
    from vaultwarden_client import VaultwardenClient
except ImportError:
    VaultwardenClient = None

# ==================== 配置 ====================
# 代理配置 (留空则不使用)
# 格式: socks5://user:pass@host:port 或 http://user:pass@host:port
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()

# 固定登录入口，OAuth后会自动跳转到实际区域
LOGIN_ENTRY_URL = "https://us-east-1.run.claw.cloud/login"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30  # Mobile验证 默认等 30 秒
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))  # 2FA验证 默认等 120 秒


class Telegram:
    """Telegram 通知"""
    
    def __init__(self):
        self.token = os.environ.get('TG_BOT_TOKEN')
        self.chat_id = os.environ.get('TG_CHAT_ID')
        self.ok = bool(self.token and self.chat_id)
    
    def send(self, msg):
        if not self.ok:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=30
            )
        except Exception as e:
            print(f"[Telegram] 发送失败: {e}")

    def photo(self, path, caption=""):
        if not self.ok or not os.path.exists(path):
            return
        try:
            with open(path, 'rb') as f:
                requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"photo": f},
                    timeout=60
                )
        except Exception as e:
            print(f"[Telegram] 发送图片失败: {e}")

    def flush_updates(self):
        """刷新 offset 到最新，避免读到旧消息"""
        if not self.ok:
            return 0
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"timeout": 0},
                timeout=10
            )
            data = r.json()
            if data.get("ok") and data.get("result"):
                return data["result"][-1]["update_id"] + 1
        except Exception:
            pass
        return 0
    
    def wait_code(self, timeout=120):
        """
        等待你在 TG 里发 /code 123456
        只接受来自 TG_CHAT_ID 的消息
        """
        if not self.ok:
            return None
        
        # 先刷新 offset，避免读到旧的 /code
        offset = self.flush_updates()
        deadline = time.time() + timeout
        pattern = re.compile(r"^/code\s+(\d{6,8})$")  # 6位TOTP 或 8位恢复码也行
        
        while time.time() < deadline:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"timeout": 20, "offset": offset},
                    timeout=30
                )
                data = r.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue
                
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    chat = msg.get("chat") or {}
                    if str(chat.get("id")) != str(self.chat_id):
                        continue
                    
                    text = (msg.get("text") or "").strip()
                    m = pattern.match(text)
                    if m:
                        return m.group(1)
            
            except Exception:
                pass
            
            time.sleep(2)
        
        return None


class SecretUpdater:
    """GitHub Secret 更新器"""
    
    def __init__(self):
        self.token = os.environ.get('REPO_TOKEN')
        self.repo = os.environ.get('GITHUB_REPOSITORY')
        self.ok = bool(self.token and self.repo)
        if self.ok:
            print("✅ Secret 自动更新已启用")
        else:
            print("⚠️ Secret 自动更新未启用（需要 REPO_TOKEN）")
    
    def update(self, name, value):
        if not self.ok:
            return False
        try:
            from nacl import encoding, public
            
            headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            # 获取公钥
            r = requests.get(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key",
                headers=headers, timeout=30
            )
            if r.status_code != 200:
                return False
            
            key_data = r.json()
            pk = public.PublicKey(key_data['key'].encode(), encoding.Base64Encoder())
            encrypted = public.SealedBox(pk).encrypt(value.encode())
            
            # 更新 Secret
            r = requests.put(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                headers=headers,
                json={"encrypted_value": base64.b64encode(encrypted).decode(), "key_id": key_data['key_id']},
                timeout=30
            )
            return r.status_code in [201, 204]
        except Exception as e:
            print(f"更新 Secret 失败: {e}")
            return False


class AutoLogin:
    """自动登录"""
    
    def __init__(self):
        # 多账号支持：读取账号 ID
        self.account_id = os.environ.get('ACCOUNT_ID', '').strip()
        self.display_name = os.environ.get('DISPLAY_NAME', self.account_id or '默认账号')

        # 初始化 Vaultwarden 客户端
        self.bw = VaultwardenClient() if VaultwardenClient else None
        self.bw_item = None  # 存储找到的 Vaultwarden 条目

        # 计算 session key（多账号时使用动态 key）
        if self.account_id:
            self.session_key = f"GH_SESSION_{self.account_id.upper()}"
        else:
            self.session_key = "GH_SESSION"

        # 从环境变量获取凭据（降级方案）
        self.username = os.environ.get('GH_USERNAME')
        self.password = os.environ.get('GH_PASSWORD')
        self.gh_session = os.environ.get('GH_SESSION', '').strip()

        # 初始化其他组件
        self.tg = Telegram()
        self.secret = SecretUpdater()
        self.shots = []
        self.logs = []
        self.n = 0

        # 区域相关
        self.detected_region = 'us-east-1'  # 检测到的区域，如 "ap-southeast-1"
        self.region_base_url = 'https://us-east-1.run.claw.cloud/'  # 检测到的区域基础 URL
        
    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line)
        self.logs.append(line)
    
    def shot(self, page, name):
        self.n += 1
        f = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
        except Exception:
            pass
        return f
    
    def click(self, page, sels, desc=""):
        for s in sels:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=3000):
                    # 模拟人类随机延迟
                    time.sleep(random.uniform(0.5, 1.5))
                    el.hover() # 先悬停
                    time.sleep(random.uniform(0.2, 0.5))
                    el.click()
                    self.log(f"已点击: {desc}", "SUCCESS")
                    return True
            except Exception:
                pass
        return False
    
    def detect_region(self, url):
        """
        从 URL 中检测区域信息
        例如: https://ap-southeast-1.console.claw.cloud/... -> ap-southeast-1
        """
        try:
            parsed = urlparse(url)
            host = parsed.netloc  # 如 "ap-southeast-1.console.claw.cloud"
            
            # 检查是否是区域子域名格式
            # 格式: {region}.console.claw.cloud
            if host.endswith('.console.claw.cloud'):
                region = host.replace('.console.claw.cloud', '')
                if region and region != 'console':  # 排除无效情况
                    self.detected_region = region
                    self.region_base_url = f"https://{host}"
                    self.log(f"检测到区域: {region}", "SUCCESS")
                    self.log(f"区域 URL: {self.region_base_url}", "INFO")
                    return region
            
            # 如果是主域名 console.run.claw.cloud，可能还没跳转
            if 'console.run.claw.cloud' in host or 'claw.cloud' in host:
                # 尝试从路径或其他地方提取区域信息
                # 有些平台可能在路径中包含区域，如 /region/ap-southeast-1/...
                path = parsed.path
                region_match = re.search(r'/(?:region|r)/([a-z]+-[a-z]+-\d+)', path)
                if region_match:
                    region = region_match.group(1)
                    self.detected_region = region
                    self.region_base_url = f"https://{region}.console.claw.cloud"
                    self.log(f"从路径检测到区域: {region}", "SUCCESS")
                    return region
            
            self.log(f"未检测到特定区域，使用当前域名: {host}", "INFO")
            # 如果没有检测到区域，使用当前 URL 的基础部分
            self.region_base_url = f"{parsed.scheme}://{parsed.netloc}"
            return None
            
        except Exception as e:
            self.log(f"区域检测异常: {e}", "WARN")
            return None
    
    def get_base_url(self):
        """获取当前应该使用的基础 URL"""
        if self.region_base_url:
            return self.region_base_url
        return LOGIN_ENTRY_URL
    
    def get_session(self, context):
        """提取 Session Cookie"""
        try:
            for c in context.cookies():
                if c['name'] == 'user_session' and 'github' in c.get('domain', ''):
                    return c['value']
        except Exception:
            pass
        return None
    
    def save_cookie(self, value):
        """保存新 Cookie（多账号支持）"""
        if not value:
            return

        self.log(f"新 Cookie: {value[:15]}...{value[-8:]}", "SUCCESS")

        # 多账号：使用动态的 session_key
        if self.secret.update(self.session_key, value):
            self.log(f"已自动更新 {self.session_key}", "SUCCESS")
            msg = f"""🔑 <b>Cookie 已自动更新</b>

账号: {self.display_name}
用户: {self.username}
Secret: {self.session_key}"""
            self.tg.send(msg)
        else:
            # 通过 Telegram 发送
            self.tg.send(f"""🔑 <b>新 Cookie</b>

账号: {self.display_name}
用户: {self.username}

请更新 Secret <b>{self.session_key}</b> (点击查看):
<tg-spoiler>{value}</tg-spoiler>
""")
            self.log("已通过 Telegram 发送 Cookie", "SUCCESS")
    
    def wait_device(self, page):
        """等待设备验证"""
        self.log(f"需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
        self.shot(page, "设备验证")
        
        self.tg.send(f"""⚠️ <b>需要设备验证</b>

请在 {DEVICE_VERIFY_WAIT} 秒内批准：
1️⃣ 检查邮箱点击链接
2️⃣ 或在 GitHub App 批准""")
        
        if self.shots:
            self.tg.photo(self.shots[-1], "设备验证页面")
        
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if i % 5 == 0:
                self.log(f"  等待... ({i}/{DEVICE_VERIFY_WAIT}秒)")
                url = page.url
                if 'verified-device' not in url and 'device-verification' not in url:
                    self.log("设备验证通过！", "SUCCESS")
                    self.tg.send("✅ <b>设备验证通过</b>")
                    return True
                try:
                    page.reload(timeout=10000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except Exception:
                    pass
        
        if 'verified-device' not in page.url:
            return True
        
        self.log("设备验证超时", "ERROR")
        self.tg.send("❌ <b>设备验证超时</b>")
        return False
    
    def wait_two_factor_mobile(self, page):
        """等待 GitHub Mobile 两步验证批准，并把数字截图提前发到电报"""
        self.log(f"需要两步验证（GitHub Mobile），等待 {TWO_FACTOR_WAIT} 秒...", "WARN")
        
        # 先截图并立刻发出去（让你看到数字）
        shot = self.shot(page, "两步验证_mobile")
        self.tg.send(f"""⚠️ <b>需要两步验证（GitHub Mobile）</b>

请打开手机 GitHub App 批准本次登录（会让你确认一个数字）。
等待时间：{TWO_FACTOR_WAIT} 秒""")
        if shot:
            self.tg.photo(shot, "两步验证页面（数字在图里）")
        
        # 不要频繁 reload，避免把流程刷回登录页
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            
            url = page.url
            
            # 如果离开 two-factor 流程页面，认为通过
            if "github.com/sessions/two-factor/" not in url:
                self.log("两步验证通过！", "SUCCESS")
                self.tg.send("✅ <b>两步验证通过</b>")
                return True
            
            # 如果被刷回登录页，说明这次流程断了（不要硬等）
            if "github.com/login" in url:
                self.log("两步验证后回到了登录页，需重新登录", "ERROR")
                return False
            
            # 每 10 秒打印一次，并补发一次截图（防止你没看到数字）
            if i % 10 == 0 and i != 0:
                self.log(f"  等待... ({i}/{TWO_FACTOR_WAIT}秒)")
                shot = self.shot(page, f"两步验证_{i}s")
                if shot:
                    self.tg.photo(shot, f"两步验证页面（第{i}秒）")
            
            # 只在 30 秒、60 秒... 做一次轻刷新（可选，频率很低）
            if i % 30 == 0 and i != 0:
                try:
                    page.reload(timeout=30000)
                    page.wait_for_load_state('domcontentloaded', timeout=30000)
                except Exception:
                    pass
        
        self.log("两步验证超时", "ERROR")
        self.tg.send("❌ <b>两步验证超时</b>")
        return False
    
    def handle_2fa_code_input(self, page):
        """处理 TOTP 验证码输入（通过 Telegram 发送 /code 123456）"""
        self.log("需要输入验证码", "WARN")
        shot = self.shot(page, "两步验证_code")

        # 如果是 Security Key (webauthn) 页面，尝试切换到 Authenticator App
        if 'two-factor/webauthn' in page.url:
            self.log("检测到 Security Key 页面，尝试切换...", "INFO")
            try:
                # 点击 "More options"
                more_options_button = page.locator('button:has-text("More options")').first
                if more_options_button.is_visible(timeout=3000):
                    more_options_button.click()
                    self.log("已点击 'More options'", "SUCCESS")
                    time.sleep(1) # 等待菜单出现
                    self.shot(page, "点击more_options后")

                    # 点击 "Authenticator app"
                    auth_app_button = page.locator('button:has-text("Authenticator app")').first
                    if auth_app_button.is_visible(timeout=2000):
                        auth_app_button.click()
                        self.log("已选择 'Authenticator app'", "SUCCESS")
                        time.sleep(2)
                        page.wait_for_load_state('networkidle', timeout=15000)
                        shot = self.shot(page, "切换到验证码输入页") # 更新截图
            except Exception as e:
                self.log(f"切换验证方式时出错: {e}", "WARN")

        # (保留) 先尝试点击"Use an authentication app"或类似按钮（如果在 mobile 页面）
        try:
            more_options = [
                'a:has-text("Use an authentication app")',
                'a:has-text("Enter a code")',
                'button:has-text("Use an authentication app")',
                'button:has-text("Authenticator app")',
                '[href*="two-factor/app"]'
            ]
            for sel in more_options:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.click()
                        time.sleep(2)
                        page.wait_for_load_state('networkidle', timeout=15000)
                        self.log("已切换到验证码输入页面", "SUCCESS")
                        shot = self.shot(page, "两步验证_code_切换后")
                        break
                except Exception:
                    pass
        except Exception:
            pass

        # 优先从 Vaultwarden 自动获取 TOTP
        code = None
        if self.bw and self.bw.available and self.bw_item:
            self.log("尝试从 Vaultwarden 自动获取 TOTP...", "INFO")
            totp = self.bw.get_totp_from_item(self.bw_item)
            if totp:
                code = totp
                self.log("✅ 已从 Vaultwarden 自动获取 TOTP", "SUCCESS")
                self.tg.send(f"""🔑 <b>自动获取验证码</b>

账号: {self.display_name}
用户: {self.username}
来源: Vaultwarden

验证码已自动填入，无需手动操作。""")
            else:
                self.log("⚠️ Vaultwarden TOTP 获取失败", "WARN")

        # 降级：通过 Telegram 手动输入
        if not code:
            self.log("降级到 Telegram 手动输入模式", "WARN")
            self.tg.send(f"""🔐 <b>需要验证码登录</b>

账号: {self.display_name}
用户: {self.username}

请在 Telegram 里发送：
<code>/code 你的6位验证码</code>

等待时间：{TWO_FACTOR_WAIT} 秒""")
            if shot:
                self.tg.photo(shot, "两步验证页面")

            self.log(f"等待验证码（{TWO_FACTOR_WAIT}秒）...", "WARN")
            code = self.tg.wait_code(timeout=TWO_FACTOR_WAIT)

        if not code:
            self.log("等待验证码超时", "ERROR")
            self.tg.send("❌ <b>等待验证码超时</b>")
            return False

        # 不打印验证码明文，只提示收到
        self.log("收到验证码，正在填入...", "SUCCESS")
        self.tg.send("✅ 收到验证码，正在填入...")

        # 常见 OTP 输入框 selector（优先级排序）
        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[name="app_otp"]',
            'input[name="otp"]',
            'input#app_totp',
            'input#otp',
            'input[inputmode="numeric"]'
        ]

        for sel in selectors:
            try:
                el = page.locator(sel).first
                if not el.is_visible(timeout=2000):
                    continue
            except Exception:
                continue

            # 找到输入框，从这里开始不再捕获异常让循环继续
            try:
                el.click()
                time.sleep(random.uniform(0.2, 0.5))
                el.type(code, delay=random.randint(50, 150))
                self.log(f"已填入验证码（selector: {sel}）", "SUCCESS")
                time.sleep(1)

                # 优先点击 Verify 按钮，不行再 Enter
                submitted = False
                verify_btns = [
                    'button:has-text("Verify")',
                    'button[type="submit"]',
                    'input[type="submit"]'
                ]
                for btn_sel in verify_btns:
                    try:
                        btn = page.locator(btn_sel).first
                        if btn.is_visible(timeout=1000):
                            btn.click()
                            submitted = True
                            self.log("已点击 Verify 按钮", "SUCCESS")
                            break
                    except Exception:
                        pass

                if not submitted:
                    time.sleep(random.uniform(0.3, 0.8))
                    page.keyboard.press("Enter")
                    self.log("已按 Enter 提交", "SUCCESS")

                time.sleep(3)
                try:
                    page.wait_for_load_state('networkidle', timeout=15000)
                except Exception:
                    pass  # networkidle 超时是正常的，跳转成功后某些请求仍活跃
                self.shot(page, "验证码提交后")

                # 检查是否通过
                if "github.com/sessions/two-factor/" not in page.url:
                    self.log("验证码验证通过！", "SUCCESS")
                    self.tg.send("✅ <b>验证码验证通过</b>")
                    return True
                else:
                    self.log("验证码可能错误", "ERROR")
                    self.tg.send("❌ <b>验证码可能错误，请检查后重试</b>")
                    return False
            except Exception as e:
                self.log(f"填入验证码时出错: {e}", "ERROR")
                return False

        self.log("没找到验证码输入框", "ERROR")
        self.tg.send("❌ <b>没找到验证码输入框</b>")
        return False
    
    def login_github(self, page, context):
        """登录 GitHub"""
        self.log("登录 GitHub...", "STEP")
        self.shot(page, "github_登录页")
        
        try:
            # 模拟人工输入
            user_input = page.locator('input[name="login"]')
            user_input.click()
            time.sleep(random.uniform(0.3, 0.8))
            user_input.type(self.username, delay=random.randint(30, 100))

            time.sleep(random.uniform(0.5, 1.0))

            pass_input = page.locator('input[name="password"]')
            pass_input.click()
            time.sleep(random.uniform(0.3, 0.8))
            pass_input.type(self.password, delay=random.randint(30, 100))

            self.log("已输入凭据")
        except Exception as e:
            self.log(f"输入失败: {e}", "ERROR")
            return False
        
        self.shot(page, "github_已填写")
        
        try:
            page.locator('input[type="submit"], button[type="submit"]').first.click()
        except Exception:
            pass
        
        time.sleep(3)
        page.wait_for_load_state('networkidle', timeout=30000)
        self.shot(page, "github_登录后")
        
        url = page.url
        self.log(f"当前: {url}")
        
        # 设备验证
        if 'verified-device' in url or 'device-verification' in url:
            if not self.wait_device(page):
                return False
            time.sleep(2)
            page.wait_for_load_state('networkidle', timeout=30000)
            self.shot(page, "验证后")
        
        # 2FA
        if 'two-factor' in page.url:
            self.log("需要两步验证！", "WARN")
            self.shot(page, "两步验证")
            
            # GitHub Mobile：等待你在手机上批准
            if 'two-factor/mobile' in page.url:
                if not self.wait_two_factor_mobile(page):
                    return False
                # 通过后等页面稳定
                try:
                    page.wait_for_load_state('networkidle', timeout=30000)
                    time.sleep(2)
                except Exception:
                    pass

            else:
                # 其它两步验证方式（TOTP/恢复码等），尝试通过 Telegram 输入验证码
                if not self.handle_2fa_code_input(page):
                    return False
                # 通过后等页面稳定
                try:
                    page.wait_for_load_state('networkidle', timeout=30000)
                    time.sleep(2)
                except Exception:
                    pass

            # 2FA 完成后，记录当前页面状态
            current_url = page.url
            self.log(f"2FA 完成后页面: {current_url}", "INFO")

        # 错误
        try:
            err = page.locator('.flash-error').first
            if err.is_visible(timeout=2000):
                self.log(f"错误: {err.inner_text()}", "ERROR")
                return False
        except Exception:
            pass

        return True
    
    def oauth(self, page):
        """处理 OAuth"""
        if 'github.com/login/oauth/authorize' in page.url:
            self.log("处理 OAuth...", "STEP")
            self.shot(page, "oauth")
            self.click(page, ['button[name="authorize"]', 'button:has-text("Authorize")'], "授权")
            time.sleep(3)
            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass  # 授权跳转后 networkidle 可能超时，忽略
    
    def wait_redirect(self, page, wait=60):
        """等待重定向并检测区域"""
        self.log("等待重定向...", "STEP")
        last_url = ""

        for i in range(wait):
            url = page.url

            # 记录 URL 变化
            if url != last_url:
                self.log(f"页面变化: {url}", "INFO")
                last_url = url

            # 检查是否已跳转到 claw.cloud
            if 'claw.cloud' in url and 'signin' not in url.lower():
                self.log("重定向成功！", "SUCCESS")

                # 检测并记录区域
                self.detect_region(url)

                return True

            # 处理 OAuth 授权页面
            if 'github.com/login/oauth/authorize' in url:
                self.log("检测到 OAuth 授权页面，正在处理...", "INFO")
                self.oauth(page)
                time.sleep(2)
                page.wait_for_load_state('networkidle', timeout=30000)

            # 检查是否卡在 GitHub 的某个页面
            if 'github.com' in url and i > 20:
                self.log(f"注意: 仍停留在 GitHub: {url}", "WARN")
                # 尝试截图帮助诊断
                if i % 10 == 0:
                    self.shot(page, f"等待重定向_{i}秒")

            time.sleep(1)
            if i % 10 == 0:
                self.log(f"  等待... ({i}秒)")

        # 超时后提供详细的错误信息
        self.log(f"重定向超时 - 当前停留在: {page.url}", "ERROR")
        self.shot(page, "重定向超时最终状态")
        return False
    
    def keepalive(self, page):
        """保活 - 使用检测到的区域 URL"""
        self.log("保活...", "STEP")
        
        # 使用检测到的区域 URL，如果没有则使用默认
        base_url = self.get_base_url()
        self.log(f"使用区域 URL: {base_url}", "INFO")
        
        pages_to_visit = [
            (f"{base_url}/", "控制台"),
            (f"{base_url}/apps", "应用"),
        ]
        
        # 如果检测到了区域，可以额外访问一些区域特定页面
        if self.detected_region:
            self.log(f"当前区域: {self.detected_region}", "INFO")
        
        for url, name in pages_to_visit:
            try:
                page.goto(url, timeout=30000)
                page.wait_for_load_state('networkidle', timeout=15000)
                self.log(f"已访问: {name} ({url})", "SUCCESS")
                
                # 再次检测区域（以防中途跳转）
                current_url = page.url
                if 'claw.cloud' in current_url:
                    self.detect_region(current_url)
                
                time.sleep(2)
            except Exception as e:
                self.log(f"访问 {name} 失败: {e}", "WARN")
        
        self.shot(page, "完成")
    
    def notify(self, ok, err=""):
        if not self.tg.ok:
            return

        # 账号信息
        account_info = f"\n<b>账号:</b> {self.display_name}" if self.account_id else ""
        region_info = f"\n<b>区域:</b> {self.detected_region or '默认'}" if self.detected_region else ""

        msg = f"""<b>🤖 ClawCloud 自动登录</b>

<b>状态:</b> {"✅ 成功" if ok else "❌ 失败"}{account_info}
<b>用户:</b> {self.username}{region_info}
<b>时间:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"""
        
        if err:
            msg += f"\n<b>错误:</b> {err}"
        
        msg += "\n\n<b>日志:</b>\n" + "\n".join(self.logs[-6:])
        
        self.tg.send(msg)
        
        if self.shots:
            if not ok:
                for s in self.shots[-3:]:
                    self.tg.photo(s, s)
            else:
                # for s in self.shots[-3:]:
                #     self.tg.photo(s, s)
                if self.shots:
                   self.tg.photo(self.shots[-1], "完成")
    
    def run(self):
        print("\n" + "="*50)
        print("🚀 ClawCloud 自动登录")
        print("="*50 + "\n")
        
        # 多账号信息
        if self.account_id:
            self.log(f"账号: {self.display_name} (ID: {self.account_id})", "INFO")
            self.log(f"Session Secret: {self.session_key}", "INFO")

        self.log(f"用户名: {self.username or '(待从 Vaultwarden 获取)'}")
        self.log(f"Session: {'有' if self.gh_session else '无'}")
        self.log(f"密码: {'有' if self.password else '(待从 Vaultwarden 获取)'}")
        self.log(f"登录入口: {LOGIN_ENTRY_URL}")

        # 尝试从 Vaultwarden 获取凭据
        if self.bw and self.account_id:
            self.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "INFO")
            self.log("尝试从 Vaultwarden 获取凭据...", "STEP")
            if self.bw.login():
                self.log("✅ Vaultwarden 连接成功", "SUCCESS")

                # 通过 account_id 查找条目（优先 Custom Field）
                username, password, item = self.bw.get_credentials_by_account_id(self.account_id)

                if username and password:
                    self.username = username
                    self.password = password
                    self.bw_item = item
                    item_name = item.get('name', '未知') if item else '未知'
                    self.log(f"✅ 已从 Vaultwarden 获取凭据", "SUCCESS")
                    self.log(f"   条目: {item_name}", "INFO")
                    self.log(f"   用户: {self.username}", "INFO")
                else:
                    self.log(f"⚠️ 未找到账号 {self.account_id} 的凭据", "WARN")
                    self.log("   回退到环境变量", "WARN")
            else:
                self.log("⚠️ Vaultwarden 连接失败，使用环境变量", "WARN")
            self.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "INFO")

        if not self.username or not self.password:
            self.log("缺少凭据（环境变量和 Vaultwarden 都未提供）", "ERROR")
            self.notify(False, "凭据未配置")
            sys.exit(1)
        
        with sync_playwright() as p:
            # 代理配置解析
            launch_args = {
                "headless": True,
                "args": [
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--exclude-switches=enable-automation',
                ]
            }

            if PROXY_DSN:
                try:
                    p_url = urlparse(PROXY_DSN)
                    proxy_config = {
                        "server": f"{p_url.scheme}://{p_url.hostname}:{p_url.port}"
                    }
                    if p_url.username:
                        proxy_config["username"] = p_url.username
                    if p_url.password:
                        proxy_config["password"] = p_url.password

                    launch_args["proxy"] = proxy_config
                    self.log(f"启用代理: {proxy_config['server']}")
                except Exception as e:
                    self.log(f"代理配置解析失败: {e}", "ERROR")

            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
            )
            page = context.new_page()
            page.add_init_script("""
                // 基础反检测
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // 模拟插件 (Headless Chrome 默认无插件)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });

                // 模拟语言
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });

                // 模拟 window.chrome
                window.chrome = { runtime: {} };

                // 绕过权限检测
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
            """)
            
            try:
                # 预加载 Cookie
                if self.gh_session:
                    try:
                        context.add_cookies([
                            {'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'},
                            {'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'}
                        ])
                        self.log("已加载 Session Cookie", "SUCCESS")
                    except Exception:
                        self.log("加载 Cookie 失败", "WARN")
                
                # 1. 访问 ClawCloud 登录入口
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto(SIGNIN_URL, timeout=60000)
                page.wait_for_load_state('networkidle', timeout=60000)
                time.sleep(2)
                self.shot(page, "clawcloud")
                
                # 检查当前 URL，可能已经自动跳转到区域
                current_url = page.url
                self.log(f"当前 URL: {current_url}")
  
            
               # 2. 点击 GitHub
                self.log("步骤2: 点击 GitHub", "STEP")

                # 先截图，看点击前的页面状态
                self.shot(page, "点击前_页面")

                # 尝试点击 GitHub 按钮
                github_clicked = False
                selectors = [
                    'button:has-text("GitHub")',
                    'a:has-text("GitHub")',
                    '[data-provider="github"]',
                    'button[type="button"]:has-text("GitHub")',
                    '.github-button',
                    '#github-login'
                ]

                for sel in selectors:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=3000):
                            self.log(f"找到按钮: {sel}", "INFO")

                            # 确保元素可点击
                            el.scroll_into_view_if_needed()
                            time.sleep(0.5)

                            # 记录点击前的 URL
                            url_before = page.url
                            self.log(f"点击前 URL: {url_before}", "INFO")

                            # 点击并等待导航
                            try:
                                # 使用 expect_navigation 来捕获页面跳转
                                with page.expect_navigation(timeout=10000):
                                    el.click()
                                    self.log(f"已点击并检测到导航: {sel}", "SUCCESS")
                                github_clicked = True
                            except Exception as nav_error:
                                # 如果没有导航，可能是在同一页面或新窗口
                                self.log(f"点击后无导航事件: {nav_error}", "WARN")
                                el.click()
                                github_clicked = True
                                time.sleep(3)

                                # 检查是否有新窗口
                                pages = context.pages
                                if len(pages) > 1:
                                    self.log(f"检测到 {len(pages)} 个页面，使用新页面", "INFO")
                                    page = pages[-1]  # 使用最新的页面
                                    page.wait_for_load_state('domcontentloaded', timeout=30000)

                            # 等待一下确保页面稳定
                            time.sleep(2)

                            # 检查 URL 是否变化
                            url_after = page.url
                            self.log(f"点击后 URL: {url_after}", "INFO")

                            if url_before != url_after or 'github.com' in url_after:
                                self.log("跳转成功", "SUCCESS")
                            else:
                                self.log("警告: URL 未变化，可能点击未生效", "WARN")
                                # 尝试等待更长时间
                                time.sleep(5)
                                self.log(f"再次检查 URL: {page.url}", "INFO")

                            break
                    except Exception as e:
                        self.log(f"选择器 {sel} 失败: {e}", "WARN")
                        continue

                if not github_clicked:
                    self.log("找不到 GitHub 按钮", "ERROR")
                    self.shot(page, "找不到按钮")
                    self.notify(False, "找不到 GitHub 按钮")
                    sys.exit(1)

                # 检查点击后的 URL
                time.sleep(2)
                current_url = page.url
                self.log(f"点击 2 秒后 URL: {current_url}")

                # 如果到达 callback 页面，需要等待它处理（排除 oauth/authorize 中 redirect_uri 参数含 callback 的情况）
                if 'claw.cloud/callback' in current_url or (current_url.split('?')[0].endswith('/callback')):
                    self.log("检测到 callback 页面，等待处理...", "INFO")
                    self.shot(page, "callback_页面")

                    # 监控 callback 处理过程（最多等待 30 秒）
                    for i in range(30):
                        time.sleep(1)
                        url_now = page.url

                        # 检查是否完成处理
                        if '/callback' not in url_now:
                            self.log(f"Callback 处理完成，当前: {url_now}", "INFO")
                            break

                        # 每 5 秒打印一次
                        if i % 5 == 0 and i > 0:
                            self.log(f"  Callback 处理中... ({i}秒)")

                        # 检查页面是否有错误
                        try:
                            error_el = page.locator('.error, .alert-error, [role="alert"]').first
                            if error_el.is_visible(timeout=500):
                                error_text = error_el.inner_text()
                                self.log(f"Callback 错误: {error_text}", "ERROR")
                                self.shot(page, "callback_错误")
                                break
                        except Exception:
                            pass

                    self.shot(page, "callback_处理后")

                # 等待页面稳定
                time.sleep(3)
                try:
                    page.wait_for_load_state('networkidle', timeout=30000)
                except Exception as e:
                    self.log(f"等待 networkidle 超时: {e}", "WARN")

                self.shot(page, "点击后_最终")
                url = page.url
                self.log(f"最终 URL: {url}")

                if 'signin' not in url.lower() and 'claw.cloud' in url and  'github.com' not in url:
                    self.log("已登录！", "SUCCESS")
                    # 检测区域
                    self.detect_region(url)
                    self.keepalive(page)
                    # 提取并保存新 Cookie
                    new = self.get_session(context)
                    if new:
                        self.save_cookie(new)
                    self.notify(True)
                    print("\n✅ 成功！\n")
                    return
                

                
                # 3. GitHub 登录
                self.log("步骤3: GitHub 认证", "STEP")

                if ('github.com/login' in url and 'oauth/authorize' not in url) or 'github.com/session' in url:
                    if not self.login_github(page, context):
                        self.shot(page, "登录失败")
                        self.notify(False, "GitHub 登录失败")
                        sys.exit(1)

                    # 登录成功后，等待页面稳定并检查状态
                    time.sleep(3)
                    page.wait_for_load_state('networkidle', timeout=30000)
                    current_url = page.url
                    self.log(f"登录后页面: {current_url}", "INFO")
                    self.shot(page, "登录完成后")

                    # 如果在 OAuth 页面，处理授权
                    if 'github.com/login/oauth/authorize' in current_url:
                        self.log("检测到 OAuth 授权页面", "INFO")
                        self.oauth(page)
                        time.sleep(2)
                        page.wait_for_load_state('networkidle', timeout=30000)

                elif 'github.com/login/oauth/authorize' in url:
                    self.log("Cookie 有效", "SUCCESS")
                    self.oauth(page)

                # 4. 等待重定向（会自动检测区域）
                self.log("步骤4: 等待重定向", "STEP")
                if not self.wait_redirect(page):
                    self.shot(page, "重定向失败")
                    self.notify(False, "重定向失败")
                    sys.exit(1)
                
                self.shot(page, "重定向成功")
                
                # 5. 验证
                self.log("步骤5: 验证", "STEP")
                current_url = page.url
                if 'claw.cloud' not in current_url or 'signin' in current_url.lower():
                    self.notify(False, "验证失败")
                    sys.exit(1)
                
                # 再次确认区域检测
                if not self.detected_region:
                    self.detect_region(current_url)
                
                # 6. 保活（使用检测到的区域 URL）
                self.keepalive(page)
                
                # 7. 提取并保存新 Cookie
                self.log("步骤6: 更新 Cookie", "STEP")
                new = self.get_session(context)
                if new:
                    self.save_cookie(new)
                else:
                    self.log("未获取到新 Cookie", "WARN")
                
                self.notify(True)
                print("\n" + "="*50)
                print("✅ 成功！")
                if self.detected_region:
                    print(f"📍 区域: {self.detected_region}")
                print("="*50 + "\n")
                
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "异常")
                import traceback
                traceback.print_exc()
                self.notify(False, str(e))
                sys.exit(1)
            finally:
                # 清理 Vaultwarden 会话
                if self.bw and self.bw.available:
                    self.bw.logout()

                browser.close()


if __name__ == "__main__":
    AutoLogin().run()
