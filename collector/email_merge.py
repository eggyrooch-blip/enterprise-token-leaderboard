# -*- coding: utf-8 -*-
"""身份合并:把同一人的分身/外部邮箱归并到规范真人邮箱。

跨采集器共享(litellm_collector / cursor_sync 都 import 本模块),保证同一张映射表、同一逻辑。
映射来自环境变量 LITELLM_EMAIL_MERGE_MAP，格式 "src1@x:dst1@x,src2@gmail.com:dst2@x"。
键大小写不敏感。含真实员工邮箱,故只在生产 env 配置,绝不写进代码默认值(否则触发开源泄露闸)。

注意:按环境变量值惰性解析(并按原始字符串 memoize)——采集器常在 import 之后才把 .env 灌进
os.environ,故绝不能在 import 时就把映射定死;惰性读取也让单测可随时改 env 生效。
"""
from __future__ import print_function

import os

_CACHE = {}


def load_merge_map(raw):
    """解析 "src:dst,src2:dst2" → {src_lower: dst}。"""
    out = {}
    for pair in (raw or "").split(","):
        if ":" in pair:
            src, dst = pair.split(":", 1)
            if src.strip() and dst.strip():
                out[src.strip().lower()] = dst.strip()
    return out


def _current_map():
    raw = os.environ.get("LITELLM_EMAIL_MERGE_MAP", "")
    m = _CACHE.get(raw)
    if m is None:
        m = load_merge_map(raw)
        _CACHE[raw] = m
    return m


def merge_email(email):
    """把分身/外部邮箱映射到规范真人邮箱;无映射或空值原样返回(永不抛异常)。"""
    if not email:
        return email
    return _current_map().get(email.lower(), email)
