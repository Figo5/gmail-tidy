# gmail-tidy configuration reference

The configuration lives at `~/.config/gmail-tidy/config.yaml` (override the directory
with the `GMAIL_TIDY_CONFIG` environment variable). `gmail-tidy init` writes a
commented template; presets ship **disabled by default**, so a fresh install's
`scan` changes nothing.

All examples here use only synthetic addresses (`example.com`, `example.org`) —
never real senders or real inbox content.

## Top-level keys

```yaml
account: you@example.com    # optional, informational; never used to match
protect:
  include: []                # optional list of strings (label: queries or bare terms)
  exclude: []                # optional list of match blocks
rules: []                    # required-at-runtime list of rules
```

## Semantics

- **AND within one `match`:** every key that is present and non-empty must hold for
  the rule to match. There is no OR within a single `match` block.
- **OR across rules:** rules are evaluated in order and the **first** matching rule
  wins per message.
- **`protect.include`:** if non-empty, a message must be eligible to be included.
  Entries beginning with `label:` require the message to carry that label; bare terms
  require the substring to appear in the From or Subject header. If the list contains
  **any** `label:` entry, only label membership is checked (bare terms are ignored in
  that case).
- **`protect.exclude`:** a message matching ANY exclude block is **never touched**,
  even if a rule would otherwise match.
- **Protected labels are always excluded:** any message carrying `IMPORTANT`,
  `STARRED`, `SPAM`, `TRASH`, `DRAFT`, `SENT`, or `CHAT` is ineligible regardless of
  configuration. These labels can also never be named in `remove_label` — doing so is
  a config-load error (exit 2).
- **Tool labels are protected too:** any label starting with `Cleanup/` is protected.
  `remove_label` may not name a `Cleanup/*` label, and a message already carrying one
  remains eligible for **other** rules (add/archive), but its `Cleanup/*` labels are
  never removed.

## `match` keys (metadata only)

Rules match **metadata only** — never message bodies. Gmail search (`q=`) is used
purely to *narrow* what gets fetched; eligibility is always re-decided locally from
fetched metadata.

| Key | Type | Meaning |
|---|---|---|
| `category` | string | One of the presets below (e.g. `newsletters`). |
| `from_contains` | `[string]` | Any of these substrings (case-insensitive) appears in the From header. |
| `from_ends` | `[string]` | From header ends with any of these substrings (case-insensitive). |
| `to_contains` | `[string]` | Any of these substrings appears in the To header. |
| `subject_contains` | `[string]` | Any of these substrings appears in the Subject header. |
| `labels_have` | `[string]` | Message carries all of these labels. |
| `labels_missing` | `[string]` | Message carries none of these labels. |
| `older_than_days` | int | Message is at least this old (based on internal date). |
| `newer_than_days` | int | Message is at most this old. |
| `larger_than_kb` | int | Estimated size ≥ this many KiB. |
| `unread` | bool | `true` matches unread, `false` matches read. |
| `query` | string | Raw Gmail search string used for narrowing only — never the source of truth. |

> Aliases: the config keys `match_from` and `match_label` are accepted anywhere
> `from_contains` and `labels_have` are, and are normalized to the canonical names
> (spec §5 example uses `match_from`/`match_label`; the grammar uses
> `from_contains`/`labels_have`). Both spellings work; a config may not use both for
> the same block.

## Scan pagination and the checkpoint

`gmail-tidy scan --limit N` caps the plan at **N new eligible candidates** across
all rules (not raw messages fetched, and not per rule). Already-labeled,
already-archived, and excluded messages are skipped, and scanning continues past
them deeper into the mailbox.

Scan progress is persisted per rule to `checkpoint.json` in the config directory
(`~/.config/gmail-tidy/`, or `$GMAIL_TIDY_CONFIG`). It stores only opaque Gmail
`pageToken` values and rule ids — never message content. Running `scan --limit N`
repeatedly resumes where the previous scan left off, so it makes forward progress
through the mailbox until it's exhausted.

**The checkpoint is invalidated whenever `config.yaml` changes.** `scan` hashes
your rules and `protect.include`/`exclude`; if the hash differs from the stored
one, the checkpoint is discarded and the next scan restarts from page 1. This is
deliberate and safe: a stale page token under new rules could silently skip
messages. Editing config and re-scanning is always safe (scan is read-only).

## `actions` keys

| Key | Type | Meaning |
|---|---|---|
| `add_label` | `[string]` | Labels to add (nested names like `Cleanup/Newsletters` create `Cleanup` parent automatically on apply). |
| `remove_label` | `[string]` | Labels to remove. **Protected labels and `Cleanup/*` are rejected at load time (exit 2).** |
| `archive` | bool | Remove the message from INBOX (archive). |

Only these two write capabilities exist: **add/remove labels** and **archive**. There
is no delete, trash, spam-report, send, or import action anywhere in the tool.

## Presets (disabled by default)

These are shipped commented-out in the template; uncomment the block you want. They
are metadata heuristics; Gmail `category:` terms narrow the candidate fetch only.

| Preset | Effect (heuristic) |
|---|---|
| `newsletters` | From/Subject probes for `newsletter`, `digest`, `unsubscribe`; narrow with `category:updates` |
| `promotions` | Probes for `promotion`, `sale`, `offer`, `discount`; narrow with `category:promotions` |
| `receipts` | Probes for `receipt`, `order`, `invoice`, `payment`; narrow with `category:purchases` |
| `notifications` | Probes for `notification`, `alert`; narrow with `category:notifications` |
| `old_unread` | `unread: true` + `older_than_days: 90` |
| `large_messages` | `larger_than_kb: 1024` |

## Protected labels (never removed, never modified)

`IMPORTANT` · `STARRED` · `SPAM` · `TRASH` · `DRAFT` · `SENT` · `CHAT` — plus any label
beginning with `Cleanup/`.

A rule that names one in `remove_label` fails config validation **at load time**
(exit 2, with the offending rule id) — never at apply time.

## Full worked example

```yaml
account: you@example.com

# Global guardrails. include: if non-empty, a message must match at least one
# entry to be eligible. exclude: matching ANY block is never touched.
protect:
  include:
    - label:work
    - label:flagged
  exclude:
    - match_from: ["bank@example.com", "boss@example.com"]
    - match_label: ["IMPORTANT", "STARRED", "Work"]

rules:
  - id: old-unread-newsletters
    match:
      category: newsletters
      older_than_days: 30
      unread: true
    actions:
      add_label: ["Cleanup/Newsletters"]
      archive: true

  - id: personal-receipts
    match:
      subject_contains: ["Receipt", "Order"]
      from_ends: ["@example.com"]
    actions:
      add_label: ["Cleanup/Receipts"]

  - id: big-attachments
    match:
      larger_than_kb: 5120
    actions:
      add_label: ["Cleanup/Large"]
```

With this config, `gmail-tidy scan` will: exclude every message from
`bank@example.com` or `boss@example.com`, every message with `IMPORTANT`, `STARRED`,
or `Work`, and every message carrying neither the `work` nor the `flagged` label;
then apply rules in order to whatever remains. (Because the include list contains
`label:` entries, eligibility is decided by label membership only — a bare-term
include such as `newsletter` would instead require the substring to appear in the
From or Subject header.)
