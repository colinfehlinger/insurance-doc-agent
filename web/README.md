# Owner View — later

The 30-second status view: which matters are blocked, on what, and for how long.

Not started. Deliberately last — the dashboard is the easiest part to build and
the least interesting part to get right, and building it early would tempt me to
design the data model around the screen instead of around the work.

Planned when it lands:

- React SPA built to S3, served through CloudFront with Origin Access Control
  (no public bucket).
- API Gateway + Lambda reading the matter table.
- Cognito for owner authentication.
- One write path only: acknowledge or override an agent decision — the
  human-in-the-loop hook.

Infrastructure seam already exists at [`infra/lib/view-stack.ts`](../infra/lib/view-stack.ts),
which is defined but not instantiated in `bin/app.ts`.
