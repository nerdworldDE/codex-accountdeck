# Finding local threads after an account switch

Sources checked on November 15, 2026. CLI thread discovery and Desktop project organization have similar user-facing symptoms, but they are separate mechanisms.

## Confirmed CLI behavior

Codex CLI 0.168.0 introduced account filtering for new local threads in the history and resume pickers. The [release notes](https://github.com/openai/codex/releases/tag/rust-v0.168.0) identify [PR #58918](https://github.com/openai/codex/pull/58918) as the implementing change.

The PR enables `currentAccountOnly` for TUI history discovery. For scoped threads, each recorded creator user/account ID must match the active login; sharing a workspace ID alone is not sufficient. Threads from before the change, or without creator IDs, remain shared. This is documented discovery behavior, rather than evidence that the conversations were deleted. The [version-matched app-server reference](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/README.md#local-history-by-account) also states that direct access by thread ID remains available.

AccountDeck discovers local thread metadata independently of the native account filter. Its Threads tab searches titles first, then user prompts and other conversation text, and launches the selected thread by ID with the current login. It does not switch accounts during resume or change Codex's account-scope settings.

## Reports from other users

| Report | What the user describes | Relation to the CLI symptom |
| --- | --- | --- |
| [Reddit: Codex CLI sessions are now account specific](https://www.reddit.com/r/codex/comments/1xw6zrl/codex_cli_sessions_are_now_account_specific/) | Two Business accounts in the same workspace on one machine show different sessions after switching. The user can still resume another account's local session by ID. | Closest match: CLI, multiple accounts, same workspace, history hidden but direct resume available. |
| [GitHub #60509: Account switching must not hide local projects and conversation history](https://github.com/openai/codex/issues/60509) | A Windows user with two Plus accounts loses the project sidebar under account B; switching back to A restores it. | Related Desktop report about visibility after an account switch, not proof of the CLI filter being responsible. |
| [GitHub #60364: Sidebar organization disappears after switching accounts](https://github.com/openai/codex/issues/60364) | A macOS user reports lost project/section organization and thread assignments under the new login; the previous account restores the layout. | Related Desktop organization issue, with a different state path from CLI thread discovery. |
| [GitHub #54155: Projects become separate chats after switching accounts](https://github.com/openai/codex/issues/54155) | Desktop users report unavailable project grouping after switching logins, while local session files remain. | Additional reports of missing organization rather than confirmed transcript deletion. |
| [Reddit: Cannot see projects anymore in Codex Desktop](https://www.reddit.com/r/codex/comments/1xrmawq/cant_see_my_projects_anymore_in_codex_desktop_app/) | A user switching between three accounts sees project grouping under only one account after an update. | Similar multi-account workflow; concerns Desktop project grouping. |

The listed GitHub issues were open when checked. A separate issue specifically reporting the CLI 0.168.0 discovery change was not identified in the searches reviewed; the introducing PR and exact CLI Reddit report are the strongest references for that behavior.

## Filing a useful CLI report

Record the CLI version, whether the same `CODEX_HOME` is used, the account-switch sequence, whether the threads reappear under the original login, and whether direct resume by ID works. Describe the confusing discovery/visibility behavior separately from any actual inability to open a thread. Avoid publishing credentials, raw session files, private conversation content or account identifiers.
