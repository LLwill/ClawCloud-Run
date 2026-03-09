"""
Vaultwarden (Bitwarden CLI) 客户端模块
通过 subprocess 调用 bw 命令行工具获取密码和 TOTP 验证码
支持通过 Custom Field 搜索条目
"""

import json
import os
import subprocess
import time


class VaultwardenClient:
    """通过 Bitwarden CLI (bw) 与 Vaultwarden 交互"""

    def __init__(self):
        self.server_url = os.environ.get("BW_SERVER_URL", "").strip()
        self.client_id = os.environ.get("BW_CLIENTID", "").strip()
        self.client_secret = os.environ.get("BW_CLIENTSECRET", "").strip()
        self.master_password = os.environ.get("BW_PASSWORD", "").strip()
        self.session = None
        self.available = False
        self._items_cache = None

        # 检查 bw CLI 是否可用
        try:
            subprocess.run(["bw", "--version"], capture_output=True, timeout=5)
            self._cli_available = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._cli_available = False
            print("[VaultwardenClient] bw CLI 未安装，Vaultwarden 功能不可用")

    def _run(self, args, env_extra=None):
        """执行 bw 命令并返回 stdout"""
        env = os.environ.copy()
        if self.session:
            env["BW_SESSION"] = self.session
        if env_extra:
            env.update(env_extra)

        try:
            result = subprocess.run(
                ["bw"] + args,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if stderr:
                    print(f"[VaultwardenClient] bw {' '.join(args[:2])} 错误: {stderr}")
                return None
            return result.stdout.strip()
        except FileNotFoundError:
            print("[VaultwardenClient] bw 命令未找到，请先安装 Bitwarden CLI")
            return None
        except subprocess.TimeoutExpired:
            print(f"[VaultwardenClient] bw {' '.join(args[:2])} 超时")
            return None
        except Exception as e:
            print(f"[VaultwardenClient] 执行异常: {e}")
            return None

    def login(self, max_retries=1):
        """登录并解锁 Vault，支持重试"""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                print(f"[VaultwardenClient] 第 {attempt + 1} 次尝试...")
                time.sleep(3)
            result = self._do_login()
            if result:
                return True
        return False

    def _do_login(self):
        """实际登录逻辑"""
        if not self._cli_available:
            print("[VaultwardenClient] bw CLI 不可用，跳过登录")
            return False

        if not all([self.client_id, self.client_secret, self.master_password]):
            print("[VaultwardenClient] 缺少凭据环境变量 (BW_CLIENTID / BW_CLIENTSECRET / BW_PASSWORD)")
            return False

        # 先登出旧会话（清理残留状态，忽略错误）
        self._run(["logout"])

        # 配置服务器地址
        if self.server_url:
            out = self._run(["config", "server", self.server_url])
            if out is None:
                print("[VaultwardenClient] 配置服务器地址失败")
                return False
            print(f"[VaultwardenClient] 服务器: {self.server_url}")

        # API Key 登录
        env_extra = {
            "BW_CLIENTID": self.client_id,
            "BW_CLIENTSECRET": self.client_secret,
        }
        out = self._run(["login", "--apikey"], env_extra=env_extra)
        if out is None:
            print("[VaultwardenClient] 登录失败")
            return False
        print("[VaultwardenClient] 登录成功")

        # 解锁
        env_extra = {"BW_PASSWORD": self.master_password}
        out = self._run(["unlock", "--passwordenv", "BW_PASSWORD", "--raw"], env_extra=env_extra)
        if out is None:
            print("[VaultwardenClient] 解锁失败")
            return False

        # 提取 session key
        self.session = out.strip()

        if not self.session:
            print("[VaultwardenClient] 无法提取 session key")
            return False

        self.available = True
        print("[VaultwardenClient] 解锁成功")
        return True

    def list_items(self, search=None):
        """列出条目，支持搜索过滤，无搜索词时结果会缓存"""
        if not self.session:
            return None

        if search:
            args = ["list", "items", "--search", search]
            out = self._run(args)
            if out is None:
                return None
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                return None

        if self._items_cache is not None:
            return self._items_cache

        out = self._run(["list", "items"])
        if out is None:
            return None
        try:
            self._items_cache = json.loads(out)
            return self._items_cache
        except json.JSONDecodeError:
            print("[VaultwardenClient] 解析条目列表失败")
            return None

    def find_item_by_field(self, field_name, field_value):
        """通过 Custom Field 查找条目"""
        # 先尝试搜索缩小范围
        items = self.list_items(search="GitHub")
        if items:
            for item in items:
                for field in item.get('fields', []):
                    if field.get('name') == field_name and field.get('value') == field_value:
                        return item

        # 降级到全量搜索
        items = self.list_items()
        if not items:
            return None
        for item in items:
            for field in item.get('fields', []):
                if field.get('name') == field_name and field.get('value') == field_value:
                    return item
        return None

    def get_item(self, search_term):
        """按名称搜索 Vault 条目，返回解析后的 JSON 对象"""
        if not self.session:
            return None
        out = self._run(["get", "item", search_term])
        if out is None:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            print(f"[VaultwardenClient] 解析条目失败: {out[:100]}")
            return None

    def get_item_by_account_id(self, account_id):
        """
        智能查找：优先通过 account_id Custom Field 查找，降级到名称约定
        """
        # 方式1: 通过 Custom Field 查找（主要方式）
        item = self.find_item_by_field('account_id', account_id)
        if item:
            item_name = item.get('name', '未知')
            print(f"[VaultwardenClient] 通过 Custom Field 找到条目: {item_name}")
            return item

        # 方式2: 通过约定名称查找（降级方式）
        conventional_name = f"GitHub-{account_id}"
        print(f"[VaultwardenClient] Custom Field 未找到，尝试约定名称: {conventional_name}")
        item = self.get_item(conventional_name)
        if item:
            print(f"[VaultwardenClient] 通过约定名称找到条目")
            return item

        print(f"[VaultwardenClient] 未找到账号 {account_id} 的条目")
        return None

    def get_password(self, item):
        """从条目对象中提取密码"""
        if isinstance(item, str):
            # 如果传入的是名称，先获取条目
            item = self.get_item(item)

        if not item:
            return None

        login = item.get('login', {})
        return login.get('password')

    def get_username(self, item):
        """从条目对象中提取用户名"""
        if isinstance(item, str):
            item = self.get_item(item)

        if not item:
            return None

        login = item.get('login', {})
        return login.get('username')

    def get_totp_from_item(self, item):
        """从条目对象生成当前 TOTP 验证码"""
        if not item:
            return None

        item_id = item.get('id')
        if not item_id:
            return None

        # 使用 bw get totp <id> 生成当前验证码
        return self._run(["get", "totp", item_id])

    def get_totp(self, search_term):
        """获取指定条目的 TOTP 验证码（兼容方法）"""
        return self._run(["get", "totp", search_term])

    def get_credentials_by_account_id(self, account_id):
        """
        通过账号 ID 获取完整凭据
        返回 (username, password, item) 元组
        """
        item = self.get_item_by_account_id(account_id)
        if not item:
            return None, None, None

        username = self.get_username(item)
        password = self.get_password(item)

        return username, password, item

    def lock(self):
        """锁定 Vault"""
        self._run(["lock"])
        self.session = None
        print("[VaultwardenClient] 已锁定")

    def logout(self):
        """登出"""
        self._run(["logout"])
        self.session = None
        self.available = False
        print("[VaultwardenClient] 已登出")
