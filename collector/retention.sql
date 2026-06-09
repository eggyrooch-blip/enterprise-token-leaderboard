-- 保留期 / 目的限制（借鉴 Meta PAI 的 purpose limitation）。
-- 每天定时跑一次（cron / CronJob），到期数据自动清理，作为合规证据之一。
-- 默认保留 400 天，按需调整；如不同 purpose 保留期不同，可加 WHERE purpose=... 分别清理。

DELETE FROM usage_daily
WHERE usage_date < current_date - INTERVAL '400 days';
