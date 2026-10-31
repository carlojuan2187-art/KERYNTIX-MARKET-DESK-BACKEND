# FTrade language preference and localized notifications — 2026-10-31

## API behavior
- User language is restricted to `en` or `es`.
- New accounts default to English unless the registration request explicitly carries `language: "es"`.
- Existing accounts with no saved language are normalized to English in user responses.
- `PATCH /auth/me` persists the authenticated user's language preference.

## Notification behavior
- User alert queries include the saved language field.
- Signal, price, anomaly, liquidation, plan and referral notifications are localized before persistence and dispatch.
- Inbox, Telegram and Web Push payloads use the same localized message when those channels are enabled for the user.
- Unknown notification text is left unchanged rather than guessed.

## Validation
- Backend test suite: 359 passed, 5 skipped.
- The five skipped tests require disposable live MongoDB/Redis services or the optional `python-telegram-bot` package.
- `python -m compileall -q api worker test` completed successfully.
