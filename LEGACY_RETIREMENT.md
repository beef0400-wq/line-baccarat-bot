# Legacy Baccarat LINE bot retired

Owner requested retirement on 2026-10-09 Asia/Taipei so the existing official LINE account can later be reused for an independent application administrator helper.

- Runtime message processing and outbound replies disabled by LEGACY_BOT_RETIRED=True.
- /callback keeps signature verification and acknowledges valid events without processing users or sending messages.
- /health reports retired=true and version V15_RETIRED_20261009.
- Source, database, account memberships and historical data retained.
- This change does not modify the LINE account's configured webhook address or native rich menu. Connecting a replacement administrator bot is a separate step.
- Local verification: follow/text events ignored; no user processing or outbound replies; invalid signatures rejected.
- Rollback: restore app.py from commit 4f04085b201e8c7cdd5b1c5f4c8feeda78d40fb6. No database rollback needed.
