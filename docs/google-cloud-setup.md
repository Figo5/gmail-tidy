# Google Cloud OAuth setup for gmail-tidy

gmail-tidy never ships a client secret or default OAuth client. Each user creates
their own OAuth client in the Google Cloud Console, downloads the secret into the
config dir, and the CLI authenticates against it. The secret and the resulting
token are private to your machine (see [SECURITY.md](../SECURITY.md)).

All addresses in this guide are illustrative (using only synthetic
`example.com` addresses per the project's no-personal-data policy); use your own
values where indicated.

## 1. Create a Google Cloud project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one). Record the project ID.
3. On the project dashboard, click **Enable APIs and services** (or go to
   **APIs & Services → Library**) and enable the **Gmail API**.
4. If you are asked to set up billing: enabling the Gmail API is free and does not
   require billing for this use; the personal-usage scope here (`gmail.readonly`,
   `gmail.modify`, `gmail.labels`) has no cost.

## 2. Configure the OAuth consent screen

In **APIs & Services → OAuth consent screen**:

1. Choose **User type**:
   - **Internal** — best for a personal project under your own Google Workspace;
     only users in your organization can authorize.
   - **Testing** — for a personal account or anything in development; add your own
     address (and any collaborators) as a **test user**. Testing-mode credentials
     expire 7 days after creation, after which the token stops working; go through
     this setup again or re-authenticate with `gmail-tidy auth`.
   - For wider public distribution, publish the app and use self-verified scopes.
2. Fill in the required fields (app name, support email, developer contact email).
3. **Scopes:** the consent screen does not need to be pre-configured with scopes —
   the CLI requests the scopes it needs at runtime. The screen will explicitly list
   them when the user consents:
   - `https://www.googleapis.com/auth/gmail.readonly` — for `scan`, `preview`,
     `status`, `auth status`.
   - `https://www.googleapis.com/auth/gmail.modify` and
     `https://www.googleapis.com/auth/gmail.labels` — for `apply` and `undo`.

## 3. Create an OAuth Client ID

1. In **APIs & Services → Credentials**, click **Create credentials → OAuth client
   ID**.
2. **Application type:** **Desktop app** (or "Desktop client").
3. Name it (e.g. `gmail-tidy`) and create.
4. In the **OAuth client created** dialog, click **Download JSON**. The downloaded
   file contains a `client_secret` field.

## 4. Save it into the config dir

Place the downloaded file in the config dir **exactly** as `client_secret.json`:

```bash
mkdir -p ~/.config/gmail-tidy
cp ~/Downloads/client_secret_....apps.googleusercontent.com.json \
   ~/.config/gmail-tidy/client_secret.json
chmod 600 ~/.config/gmail-tidy/client_secret.json
```

The config dir is `~/.config/gmail-tidy/` (override with the `GMAIL_TIDY_CONFIG`
environment variable). On POSIX, gmail-tidy sets the dir to `0700` and the token to
`0600` itself; you should do the same for the secret.

> **Never commit `client_secret.json` (or any `client_secret*.json`), `token.json`,
> or any generated credential/run/audit files.** They are matched by `.gitignore`,
> and a CI check fails if they appear in the tree.

## 5. Authenticate

```bash
gmail-tidy init
```

This writes a commented config template (presets **disabled**) and starts a
read-only OAuth flow. A browser window opens to the Google consent screen, which
lists the requested scopes. On success the token is saved to
`~/.config/gmail-tidy/token.json` (`chmod 600`).

Verify with:

```bash
gmail-tidy auth status
```

## Scope behavior

- **Read-only commands** (`scan`, `preview`, `status`, `auth status`) require only
  `gmail.readonly`. `init` establishes this scope.
- **Write commands** (`apply`, `undo`) require `gmail.modify` **plus**
  `gmail.labels`. When the stored token lacks these scopes, the CLI deletes the
  insufficient token and runs a **fresh interactive consent** whose screen
  explicitly lists the broader scopes, then persists the new token. A token is a
  single credential, not per-command — after escalation, `scan`/`preview` reuse it.
- The token's scopes are recorded in `token.json` and checked for **sufficiency**
  before reuse: a read-only token is never silently reused for a write.

## When to re-authenticate

- After the consent screen or token expires (Testing-mode credentials expire after
  7 days): re-run `gmail-tidy init` or `gmail-tidy auth refresh` for a fresh
  read-only / escalated token.
- A `403` from Gmail produces "run `gmail-tidy auth` to re-authenticate", exit 4.
- To start completely fresh with read-only-only: `gmail-tidy auth revoke`, then
  `gmail-tidy init`.
- `gmail-tidy auth revoke` best-effort revokes server-side, always deletes the local
  `token.json`, and never deletes config or audit data.
