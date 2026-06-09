#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SPIKE:用已保存的登录态,真抓一次飞书 AI 权益用量的内部接口响应,导出结构。

目的(build 阶段的探路):
  1) 证明 CDP 旁路(page.on('response'),非页面注入 → 不触发防篡改)能稳定拿到响应体。
  2) 抓到 4 个总览接口(ai_product_info / recent_partition / overview/trend /
     overview/feature_entity_top)的真实字段,供写归一化用。
  3) 探『全员逐人』:总览只有 Top10,这里顺带打开 用量详情/用量日志,看哪条能拿全员。

它【只读、只 dump】,不写库、不上报。产物:
  - feishu_capture_raw.json   原始响应(本地分析用,gitignore,可能含姓名 → 勿提交)
  - 终端打印每个接口的 顶层 key + 数组长度 + 一条样本的 key(结构,不含敏感值)

用法:
  python feishu_login.py    # 先一次性登录
  python feishu_spike.py     # 再真抓

环境变量:
  FEISHU_STATE    登录态 json(默认 ~/.feishu/keep_state.json)— 此脚本优先用同目录 profile
  FEISHU_PROFILE  持久化 profile 目录(默认 = STATE 同级 _login_profile)
  FEISHU_OUT      原始响应 dump 路径(默认 ./feishu_capture_raw.json)
"""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE = os.environ.get("FEISHU_STATE", str(Path.home() / ".feishu" / "keep_state.json"))
PROFILE = os.environ.get("FEISHU_PROFILE", str(Path(STATE).parent / "_login_profile"))
OUT = os.environ.get("FEISHU_OUT", "feishu_capture_raw.json")
BASE = "https://keep.feishu.cn/admin/aibilling"

# 我们关心的内部接口(子串匹配)
WANT = ("ai_center/homepage/ai_product_info",
        "ai_center/homepage/recent_partition",
        "ai_center/overview/trend",
        "ai_center/overview/feature_entity_top",
        "ai_center/overview/feature",
        # 全员明细候选(详情/日志页会打的接口,先广撒网抓下来看)
        "ai_center/detail", "ai_center/member", "ai_center/dept",
        "ai_center/log", "ai_center/export", "ai_center/list")


def short(j):
    """结构摘要:顶层 key + data 形状 + 样本 key(不打印敏感值)。"""
    try:
        out = {"code": j.get("code"), "top": list(j.keys())}
        d = j.get("data")
        if isinstance(d, list):
            out["data"] = f"Array(len={len(d)})"
            if d and isinstance(d[0], dict):
                out["item_keys"] = list(d[0].keys())
        elif isinstance(d, dict):
            shp = {}
            for k, v in d.items():
                if isinstance(v, list):
                    shp[k] = f"Array(len={len(v)})" + (
                        " keys=" + ",".join((v[0] or {}).keys()) if v and isinstance(v[0], dict) else "")
                elif isinstance(v, dict):
                    shp[k] = "obj keys=" + ",".join(v.keys())
                else:
                    shp[k] = type(v).__name__
            out["data"] = shp
        else:
            out["data"] = type(d).__name__
        return out
    except Exception as e:
        return {"err": str(e)}


def main() -> int:
    if not Path(PROFILE).exists() and not Path(STATE).exists():
        print(f"❌ 没有登录态。先跑: python feishu_login.py")
        return 2

    captured = {}  # url_path -> json body
    login_broken = {"v": False}

    if not Path(STATE).exists():
        print(f"❌ 没有登录态 JSON: {STATE}。先跑 feishu_login.py"); return 2

    CDP = os.environ.get("FEISHU_CDP")  # 如 http://127.0.0.1:9222 → 连你真实 Chrome
    headless = os.environ.get("FEISHU_HEADLESS", "1") != "0"
    channel = os.environ.get("FEISHU_CHANNEL") or None
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    with sync_playwright() as p:
        browser = None
        owns_page = True  # CDP 模式下不关浏览器,只关自己开的页
        if CDP:
            # 连接已登录的真实 Chrome(Profile 1):用渲染页面那套真实会话,无 headless 检测
            browser = p.chromium.connect_over_cdp(CDP)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            print(f"(模式: connect_over_cdp {CDP} — 复用真实 Chrome 会话)")
        else:
            # 复用一次性登录建立的持久 profile(最忠实:cookie+localStorage 全在)。
            # 窗口挪到屏幕外 → 有头但无感知,规避可能的 headless 检测。
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE, headless=headless, channel=channel,
                args=["--disable-blink-features=AutomationControlled",
                      "--window-position=-3000,-3000", "--window-size=1440,900"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            owns_page = False  # persistent context 收尾时整体关
            print(f"(模式: persistent_context headless={headless} channel={channel or 'bundled'} profile={PROFILE})")

        def on_response(resp):
            url = resp.url
            if not any(w in url for w in WANT):
                return
            try:
                body = resp.json()
            except Exception:
                return
            key = url.split("/ai_center/")[-1].split("?")[0]
            captured[key] = body
            if body.get("code") in (10003,) or resp.status in (401, 403):
                login_broken["v"] = True

        page.on("response", on_response)

        # 1) 总览页 —— 触发 4 个总览接口
        page.goto(f"{BASE}/usage-overview", wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
        if "login" in page.url or "passport" in page.url or "accounts" in page.url:
            print(f"❌ 登录态失效(被重定向到 {page.url})。")
            print("   CDP 模式: 请在真实 Chrome 里确认已登录飞书后台;launch 模式: 重跑 feishu_login.py")
            page.close()
            if not CDP and browser: browser.close()
            return 3

        # 2) 用量详情(成员)—— 探全员;切到部门/应用 tab 也试
        for tab in ("member", "dept", "app"):
            try:
                page.goto(f"{BASE}/usage-detail?tab={tab}", wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
            except Exception as e:
                print(f"detail tab={tab} 打开失败: {e}")

        # 3) 用量日志 —— 事件级,可能是全员来源
        try:
            page.goto(f"{BASE}/usage-log", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        except Exception as e:
            print(f"usage-log 打开失败: {e}")

        if CDP:
            page.close()  # 只关自己开的标签,不动你的真实 Chrome
        else:
            try:
                ctx.storage_state(path=STATE)  # 存回轮换后的登录态
            except Exception as e:
                print(f"(存回登录态失败,不影响本次抓取: {e})")
            ctx.close()
            if browser: browser.close()

    if login_broken["v"]:
        print("⚠️ 抓到 401/10003 —— 登录态可能半失效,结果存疑,建议重登。")

    Path(OUT).write_text(json.dumps(captured, ensure_ascii=False, indent=2))
    print(f"\n✅ 抓到 {len(captured)} 个接口,原始响应已存: {OUT}")
    print("（含姓名等敏感字段,已被 .gitignore,勿提交）\n")
    print("=== 各接口结构摘要(供写归一化) ===")
    for k in sorted(captured):
        print(f"\n[{k}]")
        print(json.dumps(short(captured[k]), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
