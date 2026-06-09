#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性登录 → 保存飞书后台登录态(storageState)。

为什么要它:飞书 AI 权益用量没有官方 API,只能从管理后台抓;后台需要登录态,
且光带 cookie 直连会 401(要 SPA 的 CSRF)。所以我们用 Playwright 真实浏览器,
让页面自己鉴权。这一步把"登录"这件事做一次,把登录态存成一个文件,之后无头采集
反复复用;文件失效时(几周一次)再跑一次本脚本即可。绝不天天手刷。

用法(在你 Mac 上,有界面):
    python feishu_login.py
会弹出一个浏览器窗口 → 你正常登录飞书 → 进到"AI 使用管理 > 用量概览"页 →
回到终端按回车 → 登录态写到 FEISHU_STATE(默认 ~/.feishu/keep_state.json)。

环境变量:
    FEISHU_STATE   登录态文件路径(默认 ~/.feishu/keep_state.json)
    FEISHU_URL     后台用量页(默认 https://keep.feishu.cn/admin/aibilling/usage-overview)
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE = os.environ.get("FEISHU_STATE", str(Path.home() / ".feishu" / "keep_state.json"))
URL = os.environ.get("FEISHU_URL", "https://keep.feishu.cn/admin/aibilling/usage-overview")


def main() -> int:
    Path(STATE).parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # 有头浏览器,独立 profile —— 不碰你日常那个 Chrome
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(Path(STATE).parent / "_login_profile"),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        print("\n" + "=" * 60)
        print("浏览器已打开。请在窗口里登录飞书,直到看到后台『用量概览』页的数据。")
        print("看到数据后,回到这里按【回车】保存登录态。")
        print("=" * 60)
        try:
            input("登录完成后按回车继续... ")
        except EOFError:
            print("非交互环境,等待 60s 后自动保存"); page.wait_for_timeout(60000)

        # 校验:确认确实在后台域、不是登录页
        cur = page.url
        if "login" in cur or "passport" in cur or "accounts" in cur:
            print(f"⚠️ 当前还在登录页({cur}),登录态可能没建好。仍尝试保存,但建议重跑。")
        ctx.storage_state(path=STATE)
        ctx.close()
    print(f"✅ 登录态已保存: {STATE}")
    print("接下来可跑:  python feishu_spike.py    (真抓一次,导出响应结构)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
