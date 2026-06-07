# YouTube Analytics MCP

A custom MCP server that closes the gap in the existing `youtube` MCP, which
only returns **all-time channel totals**. This server wraps the
**YouTube Analytics API v2** (`reports.query`) to provide:

- Date-ranged channel performance (day-by-day views, watch time, likes, subs)
- Per-video **impressions & click-through rate (CTR)** — for thumbnail/title testing
- **Audience retention curves** (where viewers drop off, second by second)
- **Traffic source breakdowns** (search vs. suggested vs. external vs. playlists)
- **Top-videos leaderboards** for any custom date range (not just all-time)
- **Viewer demographics** (age group / gender)
- A **custom query** tool for any other metric/dimension combination in the API

This is read-only — `yt-analytics.readonly` scope only. It cannot modify anything on YouTube.

---

## 1. Get OAuth credentials (one-time setup)

The server authenticates as a Google user with the
`https://www.googleapis.com/auth/yt-analytics.readonly` scope. You need three values:
a **Client ID**, a **Client Secret**, and a **Refresh Token**.

### Option A — reuse the existing Google Cloud project (recommended)

If the `youtube` MCP or `google-docs` MCP is already connected and working, a
Google Cloud project + OAuth client already exists for this account. The
fastest path:

1. Open [Google Cloud Console → APIs & Services → Library](https://console.cloud.google.com/apis/library) for that same project.
2. Search for **"YouTube Analytics API"** and click **Enable** (the YouTube
   Data API is a separate product — Analytics needs its own toggle).
3. Go to **APIs & Services → Credentials** and open the existing OAuth 2.0
   Client ID used by the other YouTube/Google integrations. Copy its
   **Client ID** and **Client Secret**.
4. You'll still need a fresh **refresh token that includes the
   `yt-analytics.readonly` scope** — existing tokens minted for the Data API
   alone won't carry this scope. Run the consent flow below (step 2) with
   that same Client ID/Secret to mint one.

### Option B — create a new OAuth client from scratch

1. In [Google Cloud Console](https://console.cloud.google.com/), create or
   select a project, then enable the **YouTube Analytics API**.
2. Go to **APIs & Services → OAuth consent screen** and configure it (External,
   add your Google account as a test user if the app is in "Testing" mode).
3. Go to **Credentials → Create Credentials → OAuth client ID**, type
   **Desktop app**, and download the Client ID + Client Secret.

### 2. Run the consent flow to mint a refresh token

With `CLIENT_ID`/`CLIENT_SECRET` from either option above, run this locally
once (requires `google-auth-oauthlib`: `pip install google-auth-oauthlib`):

```python
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": "YOUR_CLIENT_ID",
            "client_secret": "YOUR_CLIENT_SECRET",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    },
    scopes=["https://www.googleapis.com/auth/yt-analytics.readonly"],
)
creds = flow.run_local_server(port=0)
print("Refresh token:", creds.refresh_token)
```

This opens a browser, you sign in as the channel owner/manager and grant
"View YouTube Analytics reports", and it prints a refresh token. Save it —
it does not expire unless revoked.

> **Important:** sign in with the Google account that **owns or manages**
> the Zoltan Gero channel — analytics permissions follow the channel's
> account/brand-account managers, the same as in YouTube Studio.

---

## 2. Configure environment variables

Set these wherever the MCP server is launched (e.g. in the Cowork MCP config
or your shell profile):

| Variable | Required | Description |
|---|---|---|
| `YT_ANALYTICS_CLIENT_ID` | Yes | OAuth 2.0 client ID |
| `YT_ANALYTICS_CLIENT_SECRET` | Yes | OAuth 2.0 client secret |
| `YT_ANALYTICS_REFRESH_TOKEN` | Yes | Refresh token from the consent flow above (must include `yt-analytics.readonly` scope) |
| `YT_ANALYTICS_CHANNEL_ID` | No | Default channel ID (e.g. `UCxxxxxxxx`). If omitted, the server queries `channel==MINE` — the authenticated account's own channel, which is correct for Zoltan's setup. |

---

## 3. Install dependencies & run

```bash
cd youtube_analytics_mcp
pip install -r requirements.txt
python youtube_analytics_mcp.py
```

To register it with Claude / Cowork as a local stdio MCP server, point the
MCP config at:

```
command: python
args: ["/full/path/to/youtube_analytics_mcp.py"]
env:
  YT_ANALYTICS_CLIENT_ID: "..."
  YT_ANALYTICS_CLIENT_SECRET: "..."
  YT_ANALYTICS_REFRESH_TOKEN: "..."
  YT_ANALYTICS_CHANNEL_ID: "..."   # optional
```

---

## 4. Tools

| Tool | Use it for |
|---|---|
| `youtube_analytics_channel_summary` | Daily channel trend (views, watch time, likes, subs) over any date range — Agent 06's weekly review |
| `youtube_analytics_video_performance` | Impressions & CTR for specific videos — thumbnail/title A/B analysis |
| `youtube_analytics_traffic_sources` | Where views come from (search, suggested, external, playlists, …) |
| `youtube_analytics_audience_retention` | Second-by-second retention curve for one video — find drop-off points |
| `youtube_analytics_top_videos` | Leaderboard of best-performing videos for a custom date range (not all-time) |
| `youtube_analytics_demographics` | Age/gender breakdown of the audience |
| `youtube_analytics_custom_query` | Escape hatch — any metric/dimension/filter combo in the Analytics API (country, device, OS, subscription status, etc.) |

All tools accept `response_format: "markdown"` (default, human-readable table)
or `"json"` (for programmatic chaining by other agents).

---

## 5. Notes & limits

- Analytics data has roughly a **2-day processing lag** — tools default their
  date ranges to end 2 days ago.
- `impressions` / `impressionsClickThroughRate` are **only available when
  filtered to specific video IDs** — that's why
  `youtube_analytics_video_performance` requires `video_ids`.
- Demographic and retention data can be sparse for low-traffic videos; the
  API simply omits rows below its minimum sample-size threshold.
- This server is **read-only by design** (`yt-analytics.readonly`) — it
  cannot publish, edit, or delete anything.

---

## 6. Suggested integration point

Wire this into **Agent 06 (Analytics Reviewer)** — replace its current
all-time-only YouTube MCP calls with date-ranged queries from this server
for the weekly Sunday 01:00 ICT review (per `CLAUDE.md` pipeline rules), and
update the `_System_Notes_and_Limitations` doc to note the gap is now closed.
