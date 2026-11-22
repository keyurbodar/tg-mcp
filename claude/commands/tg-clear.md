# Telegram Ops Triage Protocol

1. Call `get_pending_tg_pings` with `hours_back: 120` to capture the backlog. If `truncated` is true, work through this batch, then call again.
2. Drop non-actionable chatter, greetings, and resolved items. Collect their `raw_tg_id`s.
3. Group the remaining messages by chat title.
4. For each action item:
   - Give a 1-line summary of what the teammate is asking.
   - Draft a concise reply in the user's own tone:
     * no corporate fluff or polite filler ("thanks", "hope you're well")
     * lowercase, informal punctuation, like crypto team chat
     * direct and unblocking
5. Present the items as one numbered list and wait for the user ("send 1, 3", or edits).
6. Call `send_tg_reply` only for the approved numbers, with the exact approved text, using each item's `chat_id` and `message_id` as `reply_to_msg_id`.
7. Call `mark_tg_handled` with the chatter ids from step 2 and any items the user dismissed.
8. For partner updates or alpha the user wants kept, call `route_tg_to_hub_block` (`partner_briefs` or `signals`).
