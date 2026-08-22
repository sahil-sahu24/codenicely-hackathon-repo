# Agent instructions

## Always follow this file

Every task must follow every instruction in this file. Do not skip sections. Do not treat any rule as optional. If a user request conflicts with this file, follow this file and tell the user.

## Do every task very fast

Whatever task the user gives, do it immediately and as fast as possible.

1. Start the work in the same turn. Do not wait, do not recap the whole project, and do not ask permission to begin.
2. Skip extra discovery, extra agents, and extra questions unless the task is blocked.
3. Change only what the task needs, then verify only that, then stop.
4. Prefer a short done-state over a long explanation.
5. Do not read or rewrite the whole database. Open only the table, column, query, or row the task needs. Never dump `reminders.db` or rescan all stored reminders unless the user asked for that.

Before editing, fill or respect the **Areas** table below. If a path in the table does not exist in this repo, do not invent work there.

## Surgical changes only

For every new request, continue from the current project state. Do not restart discovery from the beginning and do not rescan the whole codebase.

1. Identify the single feature, function, or file directly responsible for the request.
2. Inspect only that code and its immediately required dependency or caller.
3. Touch only the smallest set of lines and files needed to complete the request.
4. Preserve all unrelated behavior and existing user changes.
5. Verify only the changed flow unless the change affects a shared contract used elsewhere.
6. Expand the search only when the first relevant location proves insufficient, and explain why before expanding.
7. Use prior conversation context and completed work; do not repeat checks that already passed unless the new change can invalidate them.

## Speed: stay in the one area that solves the request

Do the work yourself in the main conversation. Do **not** launch subagents by default. Finish the requested task in one pass whenever possible.

1. **Pick one area** from the table. If the request clearly spans two areas, pick those two only.
2. **Open only that area’s files.** Do not scan the whole repo. Do not edit other areas.
3. **Use 0 or 1 extra agent** only when blocked (unknown files, or two areas that must change in parallel). Never default to 5. Never spawn explorer + implementer + tester + reviewer for a small fix.
4. Questions, explanations, and one-file edits: no subagents.
5. After the change, verify only what you touched (that flow, those routes, those files). Do not re-audit unrelated features.
6. If unsure which area: read **only 2–3 likely files in one area**. Do not explore other areas.

Do not refactor, rename, or “clean up” files outside the chosen area.

## Areas — touch only the matching files

Customize the paths once per project. Keep 4–7 areas. Prefer an allowlist of files/folders, not “don’t touch X”.

| If the task is about | Area | Files you may change |
| --- | --- | --- |
| Layout, pages, buttons, modals, CSS, client UI (not checkout logic) | **frontend** | `src/components/**`, `src/app/**/page.*`, `src/app |
| HTTP APIs, jobs, queues, providers, prompts, generation, webhooks | **backend** | `src/lib/**`, `src/server/**`, `src/app/api/**`, `src |
| Login, session, profile, entitlements, free usage, onboarding (not checkout) | **account** | `src/lib/auth.*`, `src/lib/session.*`, `sr |
| Payments, billing, checkout, subscriptions, credit packs, applying paid credits | **payment** | See **Payments lock** below. Nowhere el |
| Shared constants, plan catalogs (except live price/charge amounts unless payment task) | **shared** | `src/shared/**`, `src/config/**` |
| DB schema, migrations, ORM client | **data** | `prisma/**`, `drizzle/**`, `src/lib/db.*` |
| CI, deploy, env examples, ignore files | **infra** | `.github/**`, `Dockerfile*`, `vercel.json`, `.env.example` |
| Telegram reminder bot, commands, scheduling, local storage | **telegram-bot** | `bot.py`, `README.md`, `.env.example`, `.gitignore`, `data/**` |

If this repo uses different folders, edit the table — do not search the whole tree to “find the real path”.

### How to pick the area

- Look/feel of a page, no price/checkout change → **frontend**
- Pack amount, Stripe/Razorpay/PayPal, applying paid credits → **payment**
- Generate / jobs / model provider / webhook payload → **backend**
- Sign-in, session, free tier, entitlements → **account**
- Schema / migration → **data**
- Deploy / CI → **infra**
- Constant used by more than one area, not a price → **shared**

## API routes stay thin

API handlers (`src/app/api/**`, `pages/api/**`, `server/routes/**`) stay **thin wrappers**:

- parse input
- auth / permission check
- call **one** domain module
- return JSON / response

Do not copy business logic into route files. Put logic in the matching area module (`src/lib`, `src/server`, `src/backend/lib`, etc.).

## Payments lock

Whenever the task is payment, billing, checkout, a payment provider, credit packs, or applying paid credits / premium:

| Layer | File (customize) | What belongs here |
| --- | --- | --- |
| Plan catalog / prices | `src/shared/plans.js` (or `src/config/plans.ts`) | Plan ids, credits, prices, currency — **only place to change |
| Server logic | `src/lib/billing.js` (or `src/backend/lib/accountAndPayment.js`) | Provider client, order create, signature / webhook ve |
| Client UI | `src/components/Billing.jsx` (or `AccountAndPayment.jsx`) | Pay button, checkout modal, pack cards, billing summary |

Do **not** add payment behavior anywhere else. Never redefine prices outside the plan catalog file.

- New providers, amount rules, signature checks, fulfillment → server billing file only.
- New pay buttons, pack cards, checkout UI → billing UI file only.
- Payment API routes stay thin wrappers around the server billing file.
- Never put provider **secret** keys in frontend code.

If this project has no payments, delete this section and the **payment** row.

## Secrets and env

- Secrets live in env / secret manager, never in client bundles.
- `.env.example` may list **names** only, never real values.
- Do not log access tokens, raw webhooks with secrets, or card data.

## Verify

- UI change: exercise the changed flow only; check the pages that share that state.
- API change: hit the changed route(s) only.
- Do not run the entire test suite unless the user asked, or the change is in **infra** / shared types that many areas import.

## User message format (prefer this)

When starting work, treat the user’s area hint as binding:

- Area: `<name>`
- Allowed files: `<paths>`
- Do not touch: `<paths>`
- Done when: `<what to verify>`

If they omit an area, pick one from the table and state it before editing.
