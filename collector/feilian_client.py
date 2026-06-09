"""飞连（Volcengine Corplink）OpenAPI 客户端 — 只读身份/终端解析。

为 token 排行榜提供"设备 ↔ 人 ↔ 部门"的服务端解析能力，取代原方案里
"靠 MDM 给每台机器下发 EMPLOYEE_EMAIL"的一步：collector 拿到一台机器的
序列号/登录名，就能反查出归属人、邮箱、部门路径、是否活跃终端。

只用到只读权限：
  - 终端管理-获取设备信息   GET /api/open/v1/device/search
  - 组织架构-获取部门信息   GET /api/open/v1/department/list
  - 组织架构-获取成员信息   GET /api/open/v2/user/list
凭证从环境变量读取（FEILIAN_ENDPOINT / FEILIAN_ACCESS_KEY_ID /
FEILIAN_ACCESS_KEY_SECRET），绝不硬编码。
"""
import json
import os
import ssl
import time
import urllib.parse
import urllib.request

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE  # 内网私有化证书，按需放开


class FeilianClient:
    def __init__(self, endpoint=None, access_key_id=None, access_key_secret=None):
        self.endpoint = (endpoint or os.environ["FEILIAN_ENDPOINT"]).rstrip("/")
        self.akid = access_key_id or os.environ["FEILIAN_ACCESS_KEY_ID"]
        self.aks = access_key_secret or os.environ["FEILIAN_ACCESS_KEY_SECRET"]
        self._token = None
        self._token_exp = 0.0

    # ---------- 底层 ----------
    def _request(self, method, path, query=None, body=None, auth=True):
        url = f"{self.endpoint}{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Content-Type": "application/json;charset=utf-8"}
        if auth:
            headers["Authorization"] = self._access_token()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30, context=_SSL) as resp:
            payload = json.loads(resp.read().decode())
        if payload.get("code") not in (0, None):
            raise FeilianError(payload.get("code"), payload.get("message"), path)
        return payload.get("data")

    def _access_token(self):
        if self._token and time.time() < self._token_exp - 300:
            return self._token
        data = self._request(
            "POST", "/api/open/v1/token",
            body={"access_key_id": self.akid, "access_key_secret": self.aks},
            auth=False,
        )
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 7200))
        return self._token

    # ---------- 终端管理 ----------
    def device_by_serial(self, serial):
        """按 SN 精确查设备 → 归属人 + 部门 + 活跃状态。无则 None。

        一个序列号可能命中多条记录：机器在员工间转手/退还时，飞连保留新旧
        两条（旧主人 device_status=0 退还、新主人 device_status=1 在用）。
        绝不能取 devices[0]（顺序不保证，常是旧记录 → 归错到前任部门）。
        取「当前在用」的那条：device_status==1 优先于 0，再 is_live，再
        updated_time 最新。这样转手机器的用量正确归属到现任使用者。
        """
        data = self._request("GET", "/api/open/v1/device/search",
                             query={"exact_serial_number": serial, "limit": 50})
        devices = (data or {}).get("devices") or []
        if not devices:
            return None

        def _rank(d):
            return (
                1 if d.get("device_status") == 1 else 0,  # 在用 > 退还/停用
                1 if d.get("is_live") else 0,              # 在线 > 离线
                d.get("updated_time") or 0,                # 最近更新
            )

        return max(devices, key=_rank)

    def active_device_count(self, client_os=None):
        """活跃终端总数（status=1）。client_os='mac' 可只数 Mac。"""
        data = self._request("GET", "/api/open/v1/device/search",
                             query={"status": 1, "client_os": client_os, "limit": 1, "offset": 0})
        return int((data or {}).get("count") or 0)

    # ---------- 组织架构 ----------
    def department_tree(self, dept_id=None):
        return self._request("GET", "/api/open/v1/department/list",
                             query={"id": dept_id} if dept_id else None)

    def user_by_email(self, email, root_dept_id):
        """模糊搜邮箱命中唯一用户 → 邮箱/部门路径/在职状态。"""
        data = self._request("GET", "/api/open/v2/user/list",
                             query={"department_id": root_dept_id, "fetch_child": "true",
                                    "query": email, "limit": 5})
        for u in (data or {}).get("user_list") or []:
            if (u.get("email") or "").lower() == email.lower():
                return u
        return None

    def root_department_id(self):
        tree = self.department_tree()
        return (tree or [{}])[0].get("id")


class FeilianError(RuntimeError):
    def __init__(self, code, message, path):
        super().__init__(f"飞连 API {path} 失败 code={code} msg={message}")
        self.code, self.message, self.path = code, message, path
