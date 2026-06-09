#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seed the rich governance dashboard (dev_collector.py / SQLite) with neutral
synthetic data so every tab is populated — for the 5-minute local demo.

Usage:
    DEV_DB=/tmp/tok-demo.db python3 seed_dev_demo.py
    DEV_DB=/tmp/tok-demo.db PORT=8090 python3 dev_collector.py
    open http://localhost:8090/

All data is synthetic (张三/李四 placeholder names, example.com, neutral depts).
No real names, avatars, logos or org info — safe for screenshots / demos.
"""
import os, sys, datetime, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DEV_DB", "/tmp/tok-demo.db")
import dev_collector as dc

random.seed(11)
conn = dc.db()
conn.execute("DELETE FROM usage"); conn.execute("DELETE FROM people"); conn.execute("DELETE FROM report_log")

PEOPLE = [
    ("zhangsan@example.com", "张三", "Engineering / Infrastructure"),
    ("lisi@example.com",     "李四", "Engineering / Infrastructure"),
    ("wangwu@example.com",   "王五", "Engineering / Platform"),
    ("zhaoliu@example.com",  "赵六", "Engineering / Platform"),
    ("qianqi@example.com",   "钱七", "Product / Mobile"),
    ("sunba@example.com",    "孙八", "Data / Data Platform"),
    ("zhoujiu@example.com",  "周九", "Engineering / Web"),
]
SUB = [
    ("claude", "anthropic", "claude-opus-4-8"),
    ("claude", "anthropic", "claude-sonnet-4-6"),
    ("codex",  "openai",    "gpt-5.5-codex"),
    ("gemini", "google",    "gemini-3-pro"),
]
CURSOR = ("cursor", "anthropic", "claude-sonnet-4-6")


def lt_row(email, dept, source, client, provider, model, scale):
    cl = dc._CLIENT_LABELS.get(client, client)
    inp = random.randint(30, 180) * 1_000_000 * scale // 10
    out = random.randint(8, 50) * 1_000_000 * scale // 10
    cr = inp * 6 // 10
    cw = inp // 12
    rs = random.randint(0, 6) * 1_000_000 * scale // 10
    total = inp + out + cr + cw + rs
    cost = round(total / 1_000_000 * 3.2, 2)
    msgs = random.randint(300, 2600) * scale // 10
    conn.execute(dc._UPSERT_SQL, (email, dept, "lifetime", "all", source,
        cl, provider, model, inp, out, cr, cw, rs, total, cost, msgs))


def day_rows(email, dept, client, provider, model):
    cl = dc._CLIENT_LABELS.get(client, client)
    for i in range(30):
        day = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        inp = random.randint(1, 9) * 1_000_000
        out = random.randint(1, 4) * 1_000_000
        cr = inp * 6 // 10; cw = inp // 12; rs = 0
        total = inp + out + cr + cw + rs
        conn.execute(dc._UPSERT_SQL, (email, dept, "day", day, "subscription",
            cl, provider, model, inp, out, cr, cw, rs, total,
            round(total / 1_000_000 * 3.2, 2), random.randint(20, 160)))


for idx, (email, name, dept) in enumerate(PEOPLE):
    scale = [13, 11, 10, 9, 7, 6, 5][idx]
    for (c, p, m) in SUB:
        if c == "gemini" and idx % 2:
            continue
        lt_row(email, dept, "subscription", c, p, m, scale)
    lt_row(email, dept, "cursor", *CURSOR, scale)
    day_rows(email, dept, "claude", "anthropic", "claude-opus-4-8")
    day_rows(email, dept, "codex", "openai", "gpt-5.5-codex")
    conn.execute("INSERT OR REPLACE INTO people(email,name,avatar,dept) VALUES(?,?,?,?)",
                 (email, name, "", dept))
    via = "manual" if idx == 5 else "mdm"
    conn.execute("INSERT OR REPLACE INTO report_log(serial,email,hostname,ip,via,reported_at)"
        " VALUES(?,?,?,?,?,?)", (f"SN{idx:06d}", email, f"mac-{idx}", "", via,
        datetime.datetime.now().isoformat(timespec="seconds")))

AGENTS = [("agent:ci-bot", "张三", 9), ("agent:nightly-eval", "王五", 6), ("agent:release-drafter", "钱七", 4)]
for email, owner, scale in AGENTS:
    conn.execute(dc._UPSERT_SQL, (email, "", "lifetime", "all", "litellm_agent",
        "LiteLLM", "anthropic", "claude-sonnet-4-6",
        scale*20_000_000, scale*6_000_000, scale*9_000_000, scale*1_000_000, 0,
        scale*36_000_000, round(scale*36*3.2, 2), scale*120))
    conn.execute("INSERT OR REPLACE INTO people(email,name,avatar,dept) VALUES(?,?,?,?)",
                 (email, email.split(":")[1], "", owner))

conn.commit()
n = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
print(f"seeded {n} usage rows for {len(PEOPLE)} people + {len(AGENTS)} agents into {os.environ['DEV_DB']}")
conn.close()
