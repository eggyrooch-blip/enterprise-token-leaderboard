# Feishu SSO Org Auth Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Make Feishu the source of truth for people, departments, outsourcing attribution, and dashboard authorization: supplier/outsourcing spend is attributed to the real business department, employees see only themselves, department owners see their department subtree, and admins see everything.

**Architecture:** Keep the current production shape: `dev_collector.py` + SQLite + systemd on `tokscale.gotokeep.com`. Add a pure-stdlib Feishu directory sync that writes raw directory data (`feishu_users`, `departments`), derived business attribution (`department_attributions`), dashboard-compatible people data (`people`), roles, and auth sessions. Add OAuth web login endpoints and a single authorization/scope layer used by every dashboard API. Feilian remains only a device-serial attribution fallback; once an email is known, Feishu directory data wins for name, department, status, role, and effective department attribution.

**Tech Stack:** Python 3.6-compatible standard library, SQLite, existing `BaseHTTPRequestHandler` collector, Feishu Open Platform OAuth + contact APIs, systemd timer. No new runtime dependency.

## External API Facts

- Feishu web SSO uses OAuth authorization code flow: redirect to Feishu authorize URL, receive `code` and `state`, exchange code for `user_access_token`, then read current user info.
- Server-side org sync uses the existing bot credentials `FEISHU_APP_ID / FEISHU_APP_SECRET` to get `tenant_access_token`.
- Contact sync uses Feishu contact APIs with `tenant_access_token`: department children API, department detail API, and `users/find_by_department` with pagination.
- Department children must be recursively traversed. `GET /open-apis/contact/v3/departments/{department_id}/children` accepts pagination, but `fetch_child=true` is not valid on that endpoint. `page_size=50` works; `page_size=100` returns Feishu field validation error.
- Department user sync uses `GET /open-apis/contact/v3/users/find_by_department` with `fetch_child=true/false`. `/contact/v3/departments/{id}/users` is not a valid endpoint.
- Feishu department objects can include `leader_user_id`, `leaders`, `chat_id`, `member_count`, and status. With `user_id_type=open_id`, department `leader_user_id` is an `open_id`; directory storage and joins must preserve that ID space explicitly.
- Supplier/outsourcing users can have empty `email`; store them in `feishu_users` keyed by Feishu `open_id` and only mirror into `people` when a stable email exists.
- Department group chat owner is not a reliable first-pass API source: `im/v1/chats/{chat_id}` may return only partial chat data, and `im/v1/chats/{chat_id}/members` fails with `232011 Operator can NOT be out of the chat` unless the caller is in that department chat.
- Existing `tokscale` usage upsert is keyed by `email` or `sn:<serial>`, but the raw department currently comes from serial attribution. Therefore emailless supplier spend can still roll up if `_tokscale_report` maps the raw Feilian department through `department_attributions`; it must not depend on a `people` row mirrored from Feishu email.
- The Feishu app must have contact read permissions and its contact visibility range must cover all departments that should appear in the dashboard.

Docs checked:
- `https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code`
- `https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token`
- `https://open.feishu.cn/document/ukTMukTMukTM/ukDNz4SO0MjL5QzM/auth-v3/auth/tenant_access_token_internal`
- `https://open.feishu.cn/document/server-docs/contact-v3/user/find_by_department`
- `https://open.feishu.cn/document/server-docs/contact-v3/department/children`
- `lark-cli im +chat-messages-list` and `lark-cli api` checks against the Feishu group `tokscale 看板`, 2026-06-18.

## Lark Group Context Checked

Source: Feishu group `tokscale 看板`, read via `lark-cli` on 2026-06-18.

The group discussion adds a second business requirement to the SSO/Auth work:
- Current dashboard has an `外部合作商` / supplier grouping. The expected outcome is not to keep supplier spend as a standalone department, but to merge business outsourcing spend back into the actual business department.
- The preferred inference is Feishu-first: use the outsourcing department name/path to find the corresponding Feishu department, then use the supplier department's responsible person or department group owner to infer the real business department.
- Example verified from screenshots and API: `合作商 -> W -> 北京再作品牌管理有限公司(SP000083)` has supplier members without email, a supplier department `chat_id`, and no `leader_user_id`; the human-expected mapping points at the department group owner whose Feishu profile belongs to a real business department. Because IM APIs may not reveal group owner to the bot, first implementation treats this as a suggested attribution unless the owner can be resolved and promoted.
- Example verified from API: `合作商 -> W -> 中软国际科技服务有限公司(SP004867)` has a `leader_user_id`; that leader has a Keep email and a real Feishu department, so this can be automatically attributed without using group-owner lookup.
- DHR/main-data may have an interface, but the group called out higher data sensitivity and encrypted external interfaces. Treat DHR as a later/manual fallback, not the first implementation path.

## Security Decisions

- Sessions are server-side random tokens stored in SQLite, sent as `HttpOnly; SameSite=Lax` cookie.
- Session cookie name: `tok_auth`.
- Auth-required APIs return `401` if unauthenticated and `403` if authenticated but outside scope.
- State token is one-time-use and expires quickly to prevent OAuth CSRF.
- Only admins may use `include_excluded=1`, `show_departed=1`, and raw/debug endpoints.
- Dashboard static assets may load unauthenticated, but data APIs must enforce auth. The main page should redirect or show a login button when `/v1/me` returns 401.
- Do not store Feishu access tokens beyond callback handling unless needed for refresh. First version signs an internal session and discards user token.

## Data Model

Existing table:
- `people(email PRIMARY KEY, name, avatar, dept)` stays for compatibility.

Extend/create:
- `people` add nullable columns if missing:
  - `feishu_user_id TEXT`
  - `feishu_open_id TEXT`
  - `status TEXT DEFAULT 'active'`
  - `source TEXT DEFAULT ''`
  - `raw_dept TEXT DEFAULT ''`
  - `effective_dept TEXT DEFAULT ''`
  - `attribution_source TEXT DEFAULT ''`
  - `updated_at TEXT`
- `feishu_users`
  - `open_id TEXT PRIMARY KEY`
  - `user_id TEXT DEFAULT ''`
  - `union_id TEXT DEFAULT ''`
  - `email TEXT DEFAULT ''`
  - `name TEXT NOT NULL`
  - `dept_id TEXT DEFAULT ''`
  - `dept_path TEXT DEFAULT ''`
  - `status TEXT DEFAULT 'active'`
  - `employee_type TEXT DEFAULT ''`
  - `updated_at TEXT`
- `departments`
  - `dept_id TEXT PRIMARY KEY`
  - `open_dept_id TEXT DEFAULT ''`
  - `parent_id TEXT`
  - `name TEXT NOT NULL`
  - `path TEXT NOT NULL`
  - `leader_user_id TEXT DEFAULT ''`
  - `chat_id TEXT DEFAULT ''`
  - `group_owner_user_id TEXT DEFAULT ''`
  - `status TEXT DEFAULT 'active'`
  - `updated_at TEXT`
- `department_attributions`
  - `source_dept_id TEXT PRIMARY KEY`
  - `source_dept_path TEXT NOT NULL`
  - `target_dept_id TEXT DEFAULT ''`
  - `target_dept_path TEXT DEFAULT ''`
  - `rule TEXT NOT NULL` where rule is `direct_feishu_dept`, `leader_department`, `chat_owner_department`, `manual_override`, or `unresolved`
  - `confidence TEXT NOT NULL` where confidence is `high`, `medium`, or `needs_review`
  - `active INTEGER NOT NULL DEFAULT 0`
  - `reason TEXT DEFAULT ''`
  - `updated_at TEXT NOT NULL`
- `roles`
  - `email TEXT NOT NULL`
  - `role TEXT NOT NULL` where role is `admin`, `department_owner`, or `member`
  - `dept_id TEXT DEFAULT ''`
  - `dept_path TEXT DEFAULT ''`
  - `source TEXT NOT NULL`
  - `updated_at TEXT NOT NULL`
  - primary key `(email, role, dept_id)`
- `auth_states`
  - `state TEXT PRIMARY KEY`
  - `redirect TEXT`
  - `created_at INTEGER`
- `auth_sessions`
  - `sid TEXT PRIMARY KEY`
  - `email TEXT NOT NULL`
  - `created_at INTEGER`
  - `expires_at INTEGER`
  - `last_seen_at INTEGER`

Raw department and effective department are deliberately separate:
- `departments.path` and `people.raw_dept` preserve Feishu's source-of-truth org path.
- `department_attributions.target_dept_path` and `people.effective_dept` are used for dashboard spend roll-up.
- Only active, high-confidence attributions feed non-admin dashboard roll-ups by default: `direct_feishu_dept`, `leader_department`, and `manual_override`. `chat_owner_department` starts as a review suggestion unless explicitly promoted.
- When no confident attribution exists, keep the raw supplier path and mark the attribution `unresolved`; do not silently guess.

## Scope Rules

Given current user:
- `admin`: no row-level restriction. Can pass `include_excluded=1`, `show_departed=1`, and use governance/raw/debug endpoints.
- `department_owner`: visible people are those whose `people.effective_dept` (fallback `people.dept`) equals or starts with any owned `dept_path + '/'`. Can see team aggregation only for that subtree. Cannot use `include_excluded=1` unless also admin. `show_departed=1` only if explicitly allowed later.
- `member`: visible people are exactly own email. Can see personal usage, personal trend, and personal Feishu usage. Team/global aggregation returns own-only or 403, depending endpoint.

Default first-version endpoint policy:
- `/v1/leaderboard`: member sees one row; owner sees subtree; admin sees global.
- `/v1/teams`: member gets 403; owner sees subtree roll-up; admin sees global.
- `/v1/feishu`: member sees own member rows; owner sees subtree members/dept; admin sees global.
- `/v1/ai/usage?user=<self>`: member allowed for self only; owner allowed for subtree user; admin allowed all.
- `/v1/governance_metrics`: admin only in first version. Owner/member get 403 because company-wide governance can leak totals.
- `/v1/raw`: admin only.
- `/tokreport.sh`, `/tokreport.ps1`, `/v1/tokscale/report`: unchanged bearer-token machine/reporting path.

Department roll-up policy:
- Team/global spend roll-ups use `effective_dept`.
- User profile/detail can show both `raw_dept` and `effective_dept` to admins, so attribution can be audited.
- Non-admin users never see unresolved/global outsourcing buckets outside their scope.

## Implementation Tasks

### Task 1: Feishu Directory Sync Tests

**Files:**
- Create: `tests/test_feishu_directory_sync.py`
- Create later: `collector/feishu_directory_sync.py`

**Step 1: Write failing schema/idempotency tests**

Test cases:
- `ensure_tables(conn)` creates/extends `people`, `feishu_users`, `departments`, `department_attributions`, and `roles`.
- `write_directory_snapshot(conn, users, departments, admin_emails, synced_at)`:
  - writes all Feishu users keyed by Feishu `open_id`, including supplier users whose `email` is empty
  - preserves `user_id` and `union_id` as separate columns
  - mirrors only stable-email users into `people`
  - writes raw department paths and preserves `chat_id`, `leader_user_id`, `open_dept_id`
  - writes `department_owner` roles from department `leader_user_id` by joining `leader_user_id` to `feishu_users.open_id`
  - writes `admin` roles from configured admin emails
  - repeated run is idempotent
  - fails if a department leader is present but cannot be joined to an `open_id` in the current snapshot unless explicitly allowed as partial visibility

Run:
```bash
pytest tests/test_feishu_directory_sync.py -q
```
Expected before implementation: import or function missing failure.

**Step 2: Implement minimal pure-stdlib module**

Create `collector/feishu_directory_sync.py` with:
- `_json_request`
- `_get_tenant_access_token`
- `ensure_tables`
- `write_directory_snapshot`
- path builder from department parent IDs
- email-keyed `people` mirror from `feishu_users`
- explicit ID-space helpers: `open_id` for Feishu joins, `email` for local dashboard/auth keys
- `main --dry-run`

Do not call real Feishu in unit tests. Inject fake API responses.

**Step 3: Verify**

Run:
```bash
pytest tests/test_feishu_directory_sync.py -q
```
Expected: pass.

**Step 4: Commit**

```bash
git add collector/feishu_directory_sync.py tests/test_feishu_directory_sync.py
git commit -m "feat: add feishu directory sync model"
```

### Task 2: Feishu API Pagination Adapter

**Files:**
- Modify: `collector/feishu_directory_sync.py`
- Test: `tests/test_feishu_directory_sync.py`

**Step 1: Add failing tests for pagination**

Test fake `_json_request` responses for:
- tenant token
- department children pages through `GET /open-apis/contact/v3/departments/{department_id}/children`
- recursive child traversal from root `0`
- department details through `GET /open-apis/contact/v3/departments/{department_id}`
- department users pages through `GET /open-apis/contact/v3/users/find_by_department`

Assert:
- all pages are visited
- `department_id_type=department_id` and `user_id_type=open_id` are passed consistently
- user payloads store both `open_id` and `user_id`, and leader/owner resolution joins on `open_id`
- department children uses page size `50`, not `100`
- department children does not pass invalid `fetch_child`
- users endpoint may pass `fetch_child=true`
- inactive/deleted users are either marked inactive or omitted according to Feishu response fields
- fetched user count is compared to department `member_count` / `primary_member_count`; partial visibility creates a dry-run failure or explicit warning that blocks production enablement

Run targeted tests and verify failure.

**Step 2: Implement `FeishuDirectoryClient`**

Methods:
- `tenant_access_token()`
- `list_departments()`
- `list_users_by_department(dept_id, fetch_child=True)`
- `get_department(dept_id)`
- `get_user(open_id)`
- `validate_visibility_coverage(snapshot)`
- `fetch_snapshot() -> (departments, users)`

Use `tenant_access_token` only. Do not use OAuth user token for directory sync.

**Step 3: Verify**

```bash
pytest tests/test_feishu_directory_sync.py -q
```

**Step 4: Commit**

```bash
git add collector/feishu_directory_sync.py tests/test_feishu_directory_sync.py
git commit -m "feat: fetch feishu directory snapshot"
```

### Task 3: Business Outsourcing Attribution

**Files:**
- Modify: `collector/feishu_directory_sync.py`
- Test: `tests/test_feishu_directory_sync.py`

**Step 1: Write failing attribution tests**

Seed fake Feishu data:
- `合作商/W/中软国际科技服务有限公司(SP004867)` has `leader_user_id=ou_leader`; `ou_leader` has Keep email and primary department `技术平台部/固件组`.
- `合作商/W/北京再作品牌管理有限公司(SP000083)` has no leader but has `chat_id=oc_supplier`; fake IM chat lookup can return `owner_id=ou_owner`; `ou_owner` has Keep email and department `运动消费事业部/市场营销部`.
- `合作商/W/成都涉泊科技有限公司(SP006910)` has neither leader nor readable chat owner.
- Supplier users under these departments have empty `email`.

Assert:
- leader-owned supplier maps to leader's real department with rule `leader_department`, confidence `high`, `active=1`.
- readable department chat owner maps to owner department with rule `chat_owner_department`, confidence `medium`, `active=0` until manually promoted.
- unreadable/missing owner maps to `unresolved`, confidence `needs_review`, and does not rewrite people rows to a guessed department.
- non-outsourcing departments default to rule `direct_feishu_dept`.
- if a leader or chat owner resolves to another outsourcing department, the source stays `unresolved` to avoid cycles and recursive supplier-to-supplier attribution.
- an emailless supplier usage event whose raw Feilian department is `合作商/W/<supplier>` can still roll up through `department_attributions` when the attribution is active.

Run:
```bash
pytest tests/test_feishu_directory_sync.py -q
```
Expected before implementation: attribution functions missing or failing.

**Step 2: Implement attribution helpers**

Add pure functions:
- `is_outsourcing_department(path)`: true for paths under `合作商/` and future-configured prefixes.
- `derive_department_attributions(departments, users, chat_owner_lookup=None, manual_overrides=None)`.
- `effective_dept_for_person(raw_dept_path, attributions)`.

Rules in order:
1. Manual override wins if configured.
2. Non-outsourcing path maps to itself (`direct_feishu_dept`).
3. Outsourcing supplier department with `leader_user_id`: resolve leader's primary real department.
4. Outsourcing supplier department with readable `chat_id` owner: resolve owner's primary real department, but write it inactive until admin/manual promotion.
5. If the resolved target is still under any outsourcing prefix, mark unresolved.
6. Else unresolved; preserve source path.

Do not call real IM chat APIs in unit tests. Inject `chat_owner_lookup`.

**Step 3: Verify with real lark-cli smoke commands**

Record these commands in `deploy/RUNBOOK.md`; do not require them inside unit tests:
```bash
lark-cli api GET /open-apis/contact/v3/departments/4b42f1873b513cf9/children \
  --params '{"department_id_type":"department_id","user_id_type":"open_id","page_size":50}' \
  --page-all --page-limit 10

lark-cli api GET /open-apis/contact/v3/users/find_by_department \
  --params '{"department_id":"2b8321cd5b87gagd","department_id_type":"department_id","user_id_type":"open_id","page_size":20,"fetch_child":false}'
```

Expected:
- `合作商/W` supplier departments include mixed `leader_user_id`, `chat_id`, and empty cases.
- supplier users may have `email=""`.
- chat-owner suggestions are visible in admin review output but do not affect non-admin roll-ups until promoted.

**Step 4: Commit**

```bash
git add collector/feishu_directory_sync.py tests/test_feishu_directory_sync.py
git commit -m "feat: derive feishu outsourcing attribution"
```

### Task 4: Wire Directory Sync Into Deploy

**Files:**
- Modify: `deploy/deploy.sh`
- Create: `deploy/feishu-directory-sync.service`
- Create: `deploy/feishu-directory-sync.timer`
- Modify: `deploy/RUNBOOK.md`
- Modify: `deploy/.env.example`

**Step 1: Add tests or shell syntax checks**

Add or extend a deployment test if one exists. At minimum, include shell syntax verification in SIM later:
```bash
bash -n deploy/deploy.sh
```

**Step 2: Update deploy script**

Add `collector/feishu_directory_sync.py` to rsync list.

Install systemd oneshot:
- `WorkingDirectory=__REMOTE_DIR__`
- `EnvironmentFile=__REMOTE_DIR__/.env`
- `Environment=DEV_DB=__REMOTE_DIR__/tok.db`
- `ExecStart=__PYTHON__ __REMOTE_DIR__/feishu_directory_sync.py`

Timer:
- `OnBootSec=3min`
- `OnCalendar=*-*-* 02:30:00`
- `Persistent=true`

**Step 3: Document required Feishu permissions**

In RUNBOOK:
- existing `FEISHU_APP_ID / FEISHU_APP_SECRET`
- app contact visibility must cover all intended departments
- required contact read permissions
- dry-run command
- rollback: disable timer, keep last good directory snapshot

**Step 4: Verify**

```bash
bash -n deploy/deploy.sh
pytest tests/test_feishu_directory_sync.py -q
```

**Step 5: Commit**

```bash
git add deploy collector tests
git commit -m "chore: deploy feishu directory sync"
```

### Task 5: Feishu OAuth Session Tests

**Files:**
- Modify: `collector/dev_collector.py`
- Test: `tests/test_feishu_auth.py`

**Step 1: Write failing tests for auth helpers**

Test pure helper functions first:
- create state
- reject missing/expired state
- reject replayed state: consuming the same state twice must fail on the second attempt
- create session
- load current user from cookie
- session expiration
- cookie flags include `HttpOnly` and `SameSite=Lax`

Run:
```bash
pytest tests/test_feishu_auth.py -q
```
Expected: fail.

**Step 2: Implement auth table helpers**

In `dev_collector.py`, add:
- `ensure_auth_tables(conn)`
- `_new_auth_state(conn, redirect)`
- `_consume_auth_state(conn, state)`
- `_create_session(conn, email)`
- `_session_cookie(sid, max_age)`
- `_current_user(conn, headers)`

Keep helpers independent of HTTP handler where possible.

**Step 3: Verify**

```bash
pytest tests/test_feishu_auth.py -q
```

**Step 4: Commit**

```bash
git add collector/dev_collector.py tests/test_feishu_auth.py
git commit -m "feat: add feishu auth session helpers"
```

### Task 6: OAuth Routes

**Files:**
- Modify: `collector/dev_collector.py`
- Modify: `deploy/.env.example`
- Test: `tests/test_feishu_auth.py`

**Step 1: Write failing route tests**

Use a dummy handler/fake `_json_request` pattern. Test:
- `GET /auth/login` returns 302 to Feishu authorize URL with state.
- `GET /auth/callback?code=...&state=...` exchanges code and creates session.
- callback rejects invalid state.
- `GET /auth/logout` clears session cookie.
- `GET /v1/me` returns current user or 401.

**Step 2: Implement routes**

Add env vars:
- `FEISHU_OAUTH_REDIRECT_URI`
- `AUTH_SESSION_SECRET` optional only if later adding signed cookies; first version uses DB session id.
- `AUTH_ADMIN_EMAILS` comma-separated fallback admins.
- `AUTH_COOKIE_SECURE=1` for production HTTPS.
- `AUTH_ENFORCE=0|1`; `0` keeps existing data APIs open while directory sync and `/v1/me` are verified, `1` enforces 401/403 on data APIs.

OAuth exchange:
- POST `/open-apis/authen/v2/oauth/token`
- Then get user info endpoint as documented by Feishu.
- Map user to local `people` by email first, then Feishu `open_id`; never join department leaders by `user_id` when the directory snapshot was fetched with `user_id_type=open_id`.

Shadow-mode tests:
- `AUTH_ENFORCE=0`: `/v1/me` works for logged-in users, but existing data APIs keep current unauthenticated behavior.
- `AUTH_ENFORCE=1`: data APIs require session and role scope.
- Valid Feishu user not yet in synced directory becomes member/own-only if email is available; otherwise callback rejects with a clear 403 and no session.

**Step 3: Verify**

```bash
pytest tests/test_feishu_auth.py -q
```

**Step 4: Commit**

```bash
git add collector/dev_collector.py deploy/.env.example tests/test_feishu_auth.py
git commit -m "feat: add feishu oauth routes"
```

### Task 7: Authorization Scope Helpers

**Files:**
- Modify: `collector/dev_collector.py`
- Test: `tests/test_auth_scope.py`

**Step 1: Write failing tests**

Seed `people`, `departments`, `roles`, and sessions. Assert:
- admin scope has no SQL restriction
- owner scope restricts to `effective_dept = owned OR effective_dept LIKE owned || '/%'`, falling back to `dept` only when `effective_dept` is empty
- owner scope boundary is strict: owner of `技术平台部` sees `技术平台部/固件组`, but must not see `技术平台部门`
- member scope restricts to own email
- only admin can use `include_excluded=1`
- member cannot request other user through `/v1/ai/usage`

**Step 2: Implement scope model**

Add:
- `_auth_required(path)`, with machine/report endpoints exempt
- `_user_roles(conn, email)`
- `_effective_dept_expr(alias='people')`
- `_visible_email_filter(user, alias='') -> (sql, params)`
- `_visible_dept_filter(user, alias='') -> (sql, params)`
- `_can_admin_option(user, qs)`
- `_authorize_user_param(user, target_email)`

Keep SQL fragments parameterized. Do not string interpolate user input.

**Step 3: Verify**

```bash
pytest tests/test_auth_scope.py -q
```

**Step 4: Commit**

```bash
git add collector/dev_collector.py tests/test_auth_scope.py
git commit -m "feat: add dashboard authorization scopes"
```

### Task 8: Apply Scope To Dashboard APIs

**Files:**
- Modify: `collector/dev_collector.py`
- Test: `tests/test_auth_scope.py`
- Existing tests to run: `tests/test_token_review_first_four.py`, `tests/test_ai_usage_endpoint.py`, `tests/test_feishu_billing.py`, `tests/test_governance_metrics_api.py`

**Step 1: Write failing endpoint tests**

For each role, call handler methods or HTTP harness:
- member `/v1/leaderboard` only own row
- member `/v1/teams` 403
- owner `/v1/leaderboard` subtree rows only
- owner `/v1/teams` subtree roll-up only
- owner subtree includes resolved supplier spend only when `department_attributions.active=1` and `effective_dept` maps a supplier raw department into the owned business department
- owner subtree does not include inactive `chat_owner_department` suggestions until an admin/manual override promotes them
- owner subtree does not include unresolved supplier spend from outside their owned department
- owner cannot use `include_excluded=1`
- admin can use full existing behavior

**Step 2: Implement auth gate in `do_GET`**

Before route dispatch:
- static pages/assets allowed
- machine scripts allowed
- `/auth/*` and `/v1/me` allowed
- all data APIs require current user

Pass `auth_user` into relevant methods through `qs` sentinel or method parameters. Prefer method parameter if change is not too large.

**Step 3: Add SQL filters**

Apply visible filters to:
- `_personal_board_rows`
- `_leaderboard`
- `_teams`
- `_feishu`
- `_ai_usage`
- `_cursor`
- `_breakdown`
- `_governance_metrics` admin-only
- `_raw` admin-only

Any team grouping SQL should group by `COALESCE(NULLIF(people.effective_dept, ''), people.dept)`, not raw `people.dept` alone. For `sn:<serial>` or other non-email identities, `_tokscale_report` must compute `effective_dept` from the raw Feilian department through active `department_attributions` before writing lifetime/monthly/daily rows and `people`.

**Step 4: Verify targeted suite**

```bash
pytest tests/test_auth_scope.py tests/test_ai_usage_endpoint.py tests/test_feishu_billing.py tests/test_governance_metrics_api.py tests/test_token_review_first_four.py -q
```

**Step 5: Commit**

```bash
git add collector/dev_collector.py tests
git commit -m "feat: enforce role scopes on dashboard APIs"
```

### Task 9: Feilian Becomes Device Fallback

**Files:**
- Modify: `collector/dev_collector.py`
- Modify optionally: `collector/litellm_collector.py`
- Test: `tests/test_feishu_identity_source.py`

**Step 1: Write failing tests**

Cases:
- `_tokscale_report` receives serial; fake Feilian returns email and old department; existing `people` has Feishu raw/effective department; usage uses Feishu effective department.
- if Feishu person missing, Feilian department is used as temporary raw/effective fallback and people row marked source/fallback.
- later directory sync overwrites fallback people row with Feishu data.
- if Feishu raw department is a resolved supplier/outsourcing department, usage rolls up to attribution target while preserving raw department for admin audit.
- if Feilian returns no email and the identity becomes `sn:<serial>`, but returns raw department `合作商/W/<supplier>`, the usage still rolls up through active `department_attributions`.
- if the supplier attribution is inactive or unresolved, the usage keeps raw department and is visible only to admin review surfaces.

**Step 2: Implement lookup precedence**

Add helper:
- `_directory_identity_for_email(conn, email)`
- `_effective_dept_for_raw_dept(conn, raw_dept)`
- `_merge_identity(serial_identity, reported_email, directory_identity)`

Precedence:
1. Email from Feilian or payload
2. If email exists in Feishu-synced `people`, use Feishu name/avatar/raw_dept/effective_dept/status
3. Else use Feilian result and compute `effective_dept` from raw Feilian department through active `department_attributions`
4. Else `sn:<serial>`

**Step 3: Verify**

```bash
pytest tests/test_feishu_identity_source.py tests/test_dev_collector_db.py tests/test_tokscale_serial_as_list.py -q
```

**Step 4: Commit**

```bash
git add collector/dev_collector.py tests/test_feishu_identity_source.py
git commit -m "feat: prefer feishu directory over feilian org data"
```

### Task 10: Dashboard Login UX

**Files:**
- Modify: `collector/dashboard.html`
- Test: `tests/test_dashboard_auth.py` or extend `tests/test_dashboard_range.py`

**Step 1: Write failing HTML tests**

Assert dashboard contains:
- `/v1/me` bootstrap call
- login button or redirect path `/auth/login`
- role/scope label text
- no visible global data loading before auth passes

**Step 2: Implement minimal UI**

On load:
- call `/v1/me`
- if 401, render login screen with button to `/auth/login?next=/`
- if authenticated, show user name/dept/role and then load dashboard data

Do not build a full role management UI.

**Step 3: Browser SIM**

Use Playwright with fake/local sessions:
- unauthenticated page shows login state
- authenticated member sees self only
- owner sees subtree only
- admin sees full page

**Step 4: Commit**

```bash
git add collector/dashboard.html tests/test_dashboard_auth.py
git commit -m "feat: add feishu login dashboard shell"
```

### Task 11: End-to-End Tests And Deployment Plan

**Files:**
- Modify: `deploy/RUNBOOK.md`
- Modify: `.ftask/feishu-sso-org-auth/SIM_TRACE.md` later through ftask

**Step 1: Run full local tests**

```bash
pytest -q
```

Expected: all pass.

**Step 2: Run dry-run directory sync**

With real env loaded but no write:
```bash
DEV_DB=/tmp/feishu-auth-smoke.db python3 collector/feishu_directory_sync.py --dry-run
```

Expected:
- prints users/departments/roles counts
- prints attribution counts by rule: `direct_feishu_dept`, `leader_department`, `chat_owner_department`, `manual_override`, `unresolved`
- prints active vs inactive attribution counts
- prints unresolved supplier department count and sample source paths, without printing secrets
- exits 0
- no secrets printed

**Step 3: Run local dev collector SIM**

Start local collector using ftask env. Verify:
- with `AUTH_ENFORCE=0`, unauthenticated `/v1/leaderboard` keeps current behavior while `/v1/me` works for logged-in users
- with `AUTH_ENFORCE=1`, unauthenticated `/v1/leaderboard` is 401/redirect
- admin session can see global
- member session cannot see others
- owner session subtree is enforced
- active supplier spend mapped into an owned department is visible to that owner in team roll-up
- inactive chat-owner suggestions are visible only to admins
- unresolved supplier spend is visible only to admin review surfaces

**Step 4: Production deployment checklist**

Deploy only after ftask gates pass:
- `rsync` `dev_collector.py`, `dashboard.html`, `feishu_directory_sync.py`
- install `feishu-directory-sync.service/timer`
- run `sudo systemctl start feishu-directory-sync.service`
- inspect counts and logs
- inspect attribution count and unresolved supplier list
- restart `tokreport-collector`
- HTTPS verification against `https://tokscale.gotokeep.com`

**Step 5: Commit docs**

```bash
git add deploy/RUNBOOK.md
git commit -m "docs: document feishu sso org auth operations"
```

## Review Checklist Before Implementation

Ask Claude/reviewer to specifically challenge:
- Whether org sync should extend `people` in place, add `feishu_users`, or write a separate `people_directory`.
- Whether `department_attributions` is the right place to model supplier/outsourcing roll-up, instead of rewriting `people.dept`.
- Whether emailless supplier usage now has a real path from `_tokscale_report` raw Feilian department to active `department_attributions` to dashboard roll-up.
- Whether pinning Feishu joins on `open_id` covers leader/owner/OAuth mapping without mis-joining `user_id`.
- Whether the `合作商/` prefix is a safe first-version detector for outsourcing departments, or needs an explicit allowlist.
- Whether `chat_owner_department` being inactive by default is the right safety tradeoff, or whether sunke wants automatic medium-confidence roll-up.
- Whether unresolved supplier departments should remain visible to admins only, or need a public "unattributed" bucket.
- Whether `AUTH_ENFORCE=0/1` shadow mode is sufficient for production rollout and rollback.
- Whether admin/owner role derivation from Feishu department leaders is reliable enough, or needs an override table first.
- Whether `/v1/teams` for ordinary members should return own-only mini aggregation or hard 403.
- Whether session storage in SQLite is acceptable for current single-node systemd production.
- Whether any endpoint leaks global totals after row-level filtering, especially governance metrics and Feishu quota summaries.
- Whether the plan needs FastAPI/Postgres parity now or can stay scoped to production `dev_collector.py`.

## Open Assumptions To Confirm

- `AUTH_ADMIN_EMAILS` will contain at least one break-glass admin before enabling auth in production.
- The existing Feishu bot app has enough contact visibility. If not, implementation will fail dry-run until app permissions/visibility are fixed in Feishu admin.
- Department owner means Feishu department `leader_user_id`. If Keep uses another field or a separate approval owner table, adapt Tasks 1-3 before code.
- Business outsourcing departments are initially detected by raw Feishu paths under `合作商/`. If Keep has other roots for suppliers, add them to config before enabling attribution.
- Department group owner lookup may require extra IM scope and chat visibility. If unreadable, the implementation must mark those supplier departments unresolved and not guess.
- First version makes `chat_owner_department` inactive by default for non-admin roll-ups. If sunke accepts medium-confidence automatic mapping, flip that as an explicit config/manual override, not hidden behavior.
- DHR/main-data is not integrated in the first pass because the group context said its external interfaces are encrypted/high-sensitivity. It can become a later manual or service-backed fallback.
- The first production rollout uses `AUTH_ENFORCE=0` shadow mode: sync directory and expose `/v1/me`, but keep API enforcement disabled until one verified admin login works. Then flip `AUTH_ENFORCE=1`.
