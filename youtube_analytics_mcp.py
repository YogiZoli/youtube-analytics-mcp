#!/usr/bin/env python3
"""
MCP Server for the YouTube Analytics API (v2).

This server fills the gap left by the existing YouTube Data API MCP, which only
returns all-time channel totals. It exposes the YouTube Analytics `reports.query`
endpoint so agents (especially Agent 06 — Analytics Reviewer) can pull
DATE-RANGED metrics: views, watch time, CTR (impressions-based), audience
retention curves, traffic-source breakdowns, and demographics — none of which
are available through the Data API.

Auth: OAuth 2.0 user credentials with the
`https://www.googleapis.com/auth/yt-analytics.readonly` scope (read-only —
this server never writes to YouTube).

Required environment variables:
    YT_ANALYTICS_CLIENT_ID       - OAuth 2.0 client ID
    YT_ANALYTICS_CLIENT_SECRET   - OAuth 2.0 client secret
    YT_ANALYTICS_REFRESH_TOKEN   - OAuth 2.0 refresh token (already consented
                                   for the yt-analytics.readonly scope)
    YT_ANALYTICS_CHANNEL_ID      - (optional) default channel ID; if omitted,
                                   tools fall back to "channel==MINE"
"""

import json
import os
import time
from datetime import date, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

TOKEN_URL = "https://oauth2.googleapis.com/token"
ANALYTICS_API_BASE = "https://youtubeanalytics.googleapis.com/v2"
SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

CLIENT_ID = os.environ.get("YT_ANALYTICS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("YT_ANALYTICS_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("YT_ANALYTICS_REFRESH_TOKEN", "")
DEFAULT_CHANNEL_ID = os.environ.get("YT_ANALYTICS_CHANNEL_ID", "")

mcp = FastMCP("youtube_analytics_mcp")

# --------------------------------------------------------------------------
# Auth — simple in-memory access-token cache, refreshed via refresh_token
# --------------------------------------------------------------------------

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}


async def _get_access_token() -> str:
    """Return a valid OAuth access token, refreshing it if expired.

    Raises:
        RuntimeError: if required OAuth environment variables are missing,
            or if the token endpoint returns an error.
    """
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        raise RuntimeError(
            "Missing OAuth credentials. Set YT_ANALYTICS_CLIENT_ID, "
            "YT_ANALYTICS_CLIENT_SECRET and YT_ANALYTICS_REFRESH_TOKEN "
            "environment variables (a refresh token consented for the "
            f"'{SCOPE}' scope)."
        )

    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=30.0,
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to refresh OAuth access token (status {resp.status_code}): "
            f"{resp.text}. The refresh token may be expired or revoked — "
            "re-run the OAuth consent flow for the yt-analytics.readonly scope."
        )

    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + float(payload.get("expires_in", 3600))
    return _token_cache["access_token"]


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _handle_api_error(e: Exception) -> str:
    """Consistent, actionable error formatting across all tools."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            detail = e.response.json().get("error", {}).get("message", e.response.text)
        except Exception:
            detail = e.response.text
        if status == 400:
            return (
                f"Error: Bad request — {detail}. Check that your metric/dimension "
                "combination is valid for the YouTube Analytics API (some "
                "combinations, like impressions + CTR, require a `video` filter)."
            )
        if status == 401:
            return "Error: Authentication failed. The access/refresh token may be invalid or revoked."
        if status == 403:
            return (
                f"Error: Permission denied — {detail}. The authenticated account "
                "must be the owner/manager of this channel and must have granted "
                "the yt-analytics.readonly scope."
            )
        if status == 429:
            return "Error: Rate limit exceeded. Please wait before making more requests."
        return f"Error: API request failed with status {status}: {detail}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to the YouTube Analytics API timed out. Please try again."
    if isinstance(e, RuntimeError):
        return f"Error: {e}"
    return f"Error: Unexpected error occurred: {type(e).__name__}: {e}"


def _channel_filter(channel_id: Optional[str]) -> str:
    """Build the `ids` parameter — a specific channel, or 'channel==MINE'."""
    cid = (channel_id or DEFAULT_CHANNEL_ID or "").strip()
    return f"channel=={cid}" if cid else "channel==MINE"


def _validate_date(label: str, value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be in YYYY-MM-DD format (got '{value}')") from exc
    return value


async def _query_reports(
    *,
    ids: str,
    start_date: str,
    end_date: str,
    metrics: str,
    dimensions: Optional[str] = None,
    filters: Optional[str] = None,
    sort: Optional[str] = None,
    max_results: Optional[int] = None,
) -> Dict[str, Any]:
    """Call the YouTube Analytics `reports.query` endpoint and return raw JSON.

    Returns a dict with `columnHeaders` (list of {name, columnType, dataType})
    and `rows` (list of lists, positionally matching columnHeaders) — the raw
    shape returned by the API.
    """
    token = await _get_access_token()
    params: Dict[str, Any] = {
        "ids": ids,
        "startDate": start_date,
        "endDate": end_date,
        "metrics": metrics,
    }
    if dimensions:
        params["dimensions"] = dimensions
    if filters:
        params["filters"] = filters
    if sort:
        params["sort"] = sort
    if max_results:
        params["maxResults"] = max_results

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ANALYTICS_API_BASE}/reports",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()


def _rows_to_records(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert the API's positional column/row format into a list of dicts."""
    headers = [h["name"] for h in report.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in report.get("rows", [])]


def _format_response(
    *,
    title: str,
    start_date: str,
    end_date: str,
    records: List[Dict[str, Any]],
    response_format: "ResponseFormat",
    note: Optional[str] = None,
) -> str:
    if response_format == ResponseFormat.JSON:
        return json.dumps(
            {
                "title": title,
                "start_date": start_date,
                "end_date": end_date,
                "row_count": len(records),
                "rows": records,
                **({"note": note} if note else {}),
            },
            indent=2,
            default=str,
        )

    lines = [f"# {title}", "", f"Date range: {start_date} → {end_date}", f"Rows: {len(records)}"]
    if note:
        lines.append(f"\n_{note}_")
    lines.append("")
    if not records:
        lines.append("No data returned for this range/filter combination.")
        return "\n".join(lines)

    headers = list(records[0].keys())
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for rec in records[:200]:
        lines.append("| " + " | ".join(str(rec.get(h, "")) for h in headers) + " |")
    if len(records) > 200:
        lines.append(f"\n_...and {len(records) - 200} more rows. Use json format or narrower filters to see all data._")
    return "\n".join(lines)


def _default_date_range(days: int) -> tuple[str, str]:
    end = date.today() - timedelta(days=2)  # Analytics data has ~2 day processing lag
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


# --------------------------------------------------------------------------
# Pydantic input models
# --------------------------------------------------------------------------

class DateRangeMixin(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    start_date: Optional[str] = Field(
        default=None,
        description="Start date in YYYY-MM-DD format. If omitted, defaults to a recent window ending ~2 days ago (Analytics data has a processing lag).",
    )
    end_date: Optional[str] = Field(
        default=None,
        description="End date in YYYY-MM-DD format (inclusive). If omitted, defaults to ~2 days ago.",
    )
    channel_id: Optional[str] = Field(
        default=None,
        description="YouTube channel ID to report on (e.g. 'UCxxxxxxxxxxxxxxxxxx'). If omitted, uses YT_ANALYTICS_CHANNEL_ID env var, or falls back to the authenticated user's own channel ('channel==MINE').",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for a human-readable table, or 'json' for machine-readable data.",
    )

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_date("date", v)


class ChannelSummaryInput(DateRangeMixin):
    """Input for a date-ranged channel performance summary."""
    pass


class VideoPerformanceInput(DateRangeMixin):
    """Input for per-video performance, including impressions/CTR."""

    video_ids: List[str] = Field(
        ...,
        description="One or more YouTube video IDs to report on (e.g. ['dQw4w9WgXcQ']). Impressions and click-through rate are ONLY available when filtered to specific videos.",
        min_length=1,
        max_length=200,
    )

    @field_validator("video_ids")
    @classmethod
    def _check_ids(cls, v: List[str]) -> List[str]:
        cleaned = [vid.strip() for vid in v if vid.strip()]
        if not cleaned:
            raise ValueError("video_ids must contain at least one non-empty video ID")
        return cleaned


class TrafficSourcesInput(DateRangeMixin):
    """Input for a traffic-source breakdown."""

    video_id: Optional[str] = Field(
        default=None,
        description="Optional: restrict the breakdown to a single video ID. If omitted, reports traffic sources for the whole channel over the date range.",
    )
    limit: Optional[int] = Field(
        default=25, description="Maximum number of traffic-source rows to return, sorted by views descending.", ge=1, le=100,
    )


class RetentionInput(BaseModel):
    """Input for an audience-retention curve for a single video."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    video_id: str = Field(..., description="YouTube video ID to analyze (e.g. 'dQw4w9WgXcQ').", min_length=5, max_length=32)
    start_date: Optional[str] = Field(
        default=None,
        description="Start date YYYY-MM-DD. Retention is fairly stable over time, so a wide historical window (e.g. since publish) gives the cleanest curve. Defaults to the last 90 days.",
    )
    end_date: Optional[str] = Field(default=None, description="End date YYYY-MM-DD (inclusive). Defaults to ~2 days ago.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format: 'markdown' or 'json'.")

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_date("date", v)


class TopVideosInput(DateRangeMixin):
    """Input for a top-videos leaderboard for a date range."""

    metric: str = Field(
        default="views",
        description="Metric to rank by. Common choices: 'views', 'estimatedMinutesWatched', 'averageViewDuration', 'subscribersGained', 'likes', 'comments', 'shares'.",
    )
    limit: Optional[int] = Field(default=10, description="Number of top videos to return.", ge=1, le=50)


class DemographicsInput(DateRangeMixin):
    """Input for viewer demographic breakdown (age group / gender)."""

    video_id: Optional[str] = Field(
        default=None,
        description="Optional: restrict to a single video ID. If omitted, reports channel-wide demographics for the date range.",
    )


class CustomQueryInput(BaseModel):
    """Input for a fully custom YouTube Analytics reports.query call — use this for anything the convenience tools don't cover."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    start_date: str = Field(..., description="Start date in YYYY-MM-DD format.")
    end_date: str = Field(..., description="End date in YYYY-MM-DD format (inclusive).")
    metrics: str = Field(
        ...,
        description="Comma-separated list of metrics, e.g. 'views,estimatedMinutesWatched,averageViewDuration'. See https://developers.google.com/youtube/analytics/metrics for the full list.",
        min_length=1,
    )
    dimensions: Optional[str] = Field(
        default=None,
        description="Comma-separated list of dimensions to group by, e.g. 'day', 'video', 'insightTrafficSourceType', 'deviceType', 'ageGroup,gender'. See https://developers.google.com/youtube/analytics/dimensions",
    )
    filters: Optional[str] = Field(
        default=None,
        description="Filter expression, e.g. 'video==dQw4w9WgXcQ' or 'country==US'. Combine multiple with ';' (AND).",
    )
    sort: Optional[str] = Field(
        default=None,
        description="Comma-separated sort spec, e.g. '-views' (descending) or 'day' (ascending).",
    )
    max_results: Optional[int] = Field(default=None, description="Maximum number of rows to return.", ge=1, le=1000)
    channel_id: Optional[str] = Field(
        default=None,
        description="YouTube channel ID. If omitted, uses YT_ANALYTICS_CHANNEL_ID env var or 'channel==MINE'.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format: 'markdown' or 'json'.")

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        return _validate_date("date", v)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool(
    name="youtube_analytics_channel_summary",
    annotations={
        "title": "YouTube Channel Performance Summary (date-ranged)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def youtube_analytics_channel_summary(params: ChannelSummaryInput) -> str:
    """Get date-ranged channel performance metrics, broken down by day.

    Unlike the YouTube Data API (which only returns all-time totals), this
    pulls views, watch time, average view duration, subscriber change, and
    engagement for a SPECIFIC date range — e.g. "last 28 days" or "May 2026" —
    broken down day by day. Use this for weekly/monthly trend analysis
    (e.g. Agent 06's analytics review).

    Args:
        params (ChannelSummaryInput): Validated input containing:
            - start_date (Optional[str]): YYYY-MM-DD, defaults to 28 days ago
            - end_date (Optional[str]): YYYY-MM-DD, defaults to ~2 days ago
            - channel_id (Optional[str]): channel to report on; defaults to the authenticated channel
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: A daily-broken-down table (or JSON) with columns: day, views,
        estimatedMinutesWatched, averageViewDuration, likes, comments, shares,
        subscribersGained, subscribersLost.

    Examples:
        - "How did my channel perform over the last 28 days, day by day?" -> default range
        - "Give me May 2026 channel stats" -> start_date='2026-05-01', end_date='2026-05-31'

    Error Handling:
        - "Error: Authentication failed" -> refresh token is invalid/revoked, re-run OAuth consent
        - "Error: Permission denied" -> authenticated account doesn't manage this channel
    """
    try:
        start, end = (
            params.start_date or _default_date_range(28)[0],
            params.end_date or _default_date_range(28)[1],
        )
        report = await _query_reports(
            ids=_channel_filter(params.channel_id),
            start_date=start,
            end_date=end,
            metrics="views,estimatedMinutesWatched,averageViewDuration,likes,comments,shares,subscribersGained,subscribersLost",
            dimensions="day",
            sort="day",
        )
        records = _rows_to_records(report)
        return _format_response(
            title="YouTube Channel Performance Summary (daily)",
            start_date=start,
            end_date=end,
            records=records,
            response_format=params.response_format,
        )
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="youtube_analytics_video_performance",
    annotations={
        "title": "Per-Video Performance incl. Impressions & CTR",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def youtube_analytics_video_performance(params: VideoPerformanceInput) -> str:
    """Get date-ranged performance for specific videos, INCLUDING impressions and click-through rate (CTR).

    This is the tool to use for thumbnail/title performance analysis — CTR and
    impressions are only obtainable from the Analytics API when filtered to
    specific video IDs (they're not available in channel-wide queries or the
    Data API). Use this to evaluate whether a new thumbnail/title improved CTR,
    or to compare videos within a playlist.

    Args:
        params (VideoPerformanceInput): Validated input containing:
            - video_ids (List[str]): one or more video IDs (required)
            - start_date / end_date (Optional[str]): date range, defaults to last 28 days
            - channel_id (Optional[str]): defaults to the authenticated channel
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: A table (or JSON) with one row per video containing: video,
        views, estimatedMinutesWatched, averageViewDuration,
        averageViewPercentage, impressions, impressionsClickThroughRate,
        likes, comments, shares, subscribersGained.

    Examples:
        - "Did the new thumbnail on video X improve CTR this month?" -> video_ids=['X'], date range = since the thumbnail change
        - "Compare these 3 videos from my AI Agents playlist" -> video_ids=['A','B','C']

    Error Handling:
        - "Error: Bad request" -> one of the video IDs may not belong to this channel, or has too little data for the date range
    """
    try:
        start, end = (
            params.start_date or _default_date_range(28)[0],
            params.end_date or _default_date_range(28)[1],
        )
        filters = "video==" + ",".join(params.video_ids)
        report = await _query_reports(
            ids=_channel_filter(params.channel_id),
            start_date=start,
            end_date=end,
            metrics=(
                "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
                "impressions,impressionsClickThroughRate,likes,comments,shares,subscribersGained"
            ),
            dimensions="video",
            filters=filters,
            sort="-views",
        )
        records = _rows_to_records(report)
        return _format_response(
            title="Per-Video Performance (incl. Impressions & CTR)",
            start_date=start,
            end_date=end,
            records=records,
            response_format=params.response_format,
            note="impressionsClickThroughRate is a ratio (e.g. 0.045 = 4.5%). Impressions/CTR reflect YouTube's home & suggested-video surfaces.",
        )
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="youtube_analytics_traffic_sources",
    annotations={
        "title": "Traffic Source Breakdown",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def youtube_analytics_traffic_sources(params: TrafficSourcesInput) -> str:
    """Break down views and watch time by traffic source (search, suggested videos, external, playlists, etc.).

    Use this to understand WHERE viewers are coming from — critical for
    deciding whether to invest in SEO/keywords (search traffic), thumbnails/
    titles (suggested-video traffic), or off-platform promotion (external
    traffic). Not available from the Data API.

    Args:
        params (TrafficSourcesInput): Validated input containing:
            - video_id (Optional[str]): restrict to one video, or omit for channel-wide
            - start_date / end_date (Optional[str]): date range, defaults to last 28 days
            - limit (Optional[int]): max rows to return, sorted by views descending
            - channel_id (Optional[str]): defaults to the authenticated channel
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: A table (or JSON) with columns: insightTrafficSourceType, views,
        estimatedMinutesWatched. Common source types include YT_SEARCH,
        SUGGESTED_VIDEO, BROWSE_FEATURES (home/subscriptions), EXTERNAL,
        PLAYLIST, NOTIFICATION, CHANNEL, END_SCREEN, NO_LINK_OTHER.

    Examples:
        - "Where is most of my traffic coming from this month?" -> channel-wide, last 28 days
        - "Is video X getting picked up by suggested videos?" -> video_id='X'

    Error Handling:
        - Empty result usually means too narrow a date range or a very new video with little data yet
    """
    try:
        start, end = (
            params.start_date or _default_date_range(28)[0],
            params.end_date or _default_date_range(28)[1],
        )
        filters = f"video=={params.video_id}" if params.video_id else None
        report = await _query_reports(
            ids=_channel_filter(params.channel_id),
            start_date=start,
            end_date=end,
            metrics="views,estimatedMinutesWatched",
            dimensions="insightTrafficSourceType",
            filters=filters,
            sort="-views",
            max_results=params.limit,
        )
        records = _rows_to_records(report)
        return _format_response(
            title="Traffic Source Breakdown" + (f" — video {params.video_id}" if params.video_id else " — channel-wide"),
            start_date=start,
            end_date=end,
            records=records,
            response_format=params.response_format,
        )
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="youtube_analytics_audience_retention",
    annotations={
        "title": "Audience Retention Curve for a Video",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def youtube_analytics_audience_retention(params: RetentionInput) -> str:
    """Get the audience-retention curve for a single video (% of viewers remaining at each point in the video).

    Returns the `elapsedVideoTimeRatio` retention curve — e.g. a row with
    elapsedVideoTimeRatio=0.4 and audienceWatchRatio=0.55 means "55% of the
    audience was still watching after 40% of the video had elapsed". Use this
    to find exactly where viewers drop off (hooks, pacing, CTAs) — not
    available from the Data API.

    Args:
        params (RetentionInput): Validated input containing:
            - video_id (str): the video to analyze (required)
            - start_date / end_date (Optional[str]): date range for the underlying
              data; retention is fairly stable, so defaults to the last 90 days
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: A table (or JSON) with rows of {elapsedVideoTimeRatio,
        audienceWatchRatio, relativeRetentionPerformance}. Ratios are 0.0-1.0;
        multiply by 100 for percentages. relativeRetentionPerformance compares
        this video's retention to similar-length YouTube videos (>0 = better
        than average, <0 = worse).

    Examples:
        - "Where do people stop watching my latest video?" -> video_id='<id>'
        - "Is the intro of video X too long?" -> look at the ratio near elapsedVideoTimeRatio=0.0-0.1

    Error Handling:
        - "No data returned" -> the video may be too new, or have too few views in the range for retention data to be available
    """
    try:
        start = params.start_date or (date.today() - timedelta(days=90)).isoformat()
        end = params.end_date or _default_date_range(1)[1]
        report = await _query_reports(
            ids=_channel_filter(None),
            start_date=start,
            end_date=end,
            metrics="audienceWatchRatio,relativeRetentionPerformance",
            dimensions="elapsedVideoTimeRatio",
            filters=f"video=={params.video_id}",
            sort="elapsedVideoTimeRatio",
        )
        records = _rows_to_records(report)
        return _format_response(
            title=f"Audience Retention Curve — video {params.video_id}",
            start_date=start,
            end_date=end,
            records=records,
            response_format=params.response_format,
            note="elapsedVideoTimeRatio and audienceWatchRatio range 0.0-1.0 (multiply by 100 for %). relativeRetentionPerformance: positive = better than similar videos, negative = worse.",
        )
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="youtube_analytics_top_videos",
    annotations={
        "title": "Top Videos Leaderboard for a Date Range",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def youtube_analytics_top_videos(params: TopVideosInput) -> str:
    """Rank the channel's videos by a chosen metric over a date range.

    Use this to find what's working RIGHT NOW (e.g. "best videos in the last
    28 days") rather than all-time — the all-time leaderboard from the Data
    API can hide recent winners behind old evergreen hits.

    Args:
        params (TopVideosInput): Validated input containing:
            - metric (str): metric to rank by, e.g. 'views', 'estimatedMinutesWatched',
              'averageViewDuration', 'subscribersGained', 'likes' (default: 'views')
            - start_date / end_date (Optional[str]): date range, defaults to last 28 days
            - limit (Optional[int]): how many videos to return (default 10)
            - channel_id (Optional[str]): defaults to the authenticated channel
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: A ranked table (or JSON) with columns: video, views,
        estimatedMinutesWatched, averageViewDuration, subscribersGained, likes —
        sorted descending by the chosen metric. The 'video' column contains
        YouTube video IDs (prefix with https://youtube.com/watch?v= to get URLs).

    Examples:
        - "What were my best videos in the last 28 days?" -> metric='views'
        - "Which videos gained the most subscribers this month?" -> metric='subscribersGained'

    Error Handling:
        - "Error: Bad request" -> the chosen metric name may be invalid; see https://developers.google.com/youtube/analytics/metrics
    """
    try:
        start, end = (
            params.start_date or _default_date_range(28)[0],
            params.end_date or _default_date_range(28)[1],
        )
        metric = params.metric.strip()
        report = await _query_reports(
            ids=_channel_filter(params.channel_id),
            start_date=start,
            end_date=end,
            metrics=f"views,estimatedMinutesWatched,averageViewDuration,subscribersGained,likes",
            dimensions="video",
            sort=f"-{metric}",
            max_results=params.limit,
        )
        records = _rows_to_records(report)
        return _format_response(
            title=f"Top Videos by {metric} ({start} → {end})",
            start_date=start,
            end_date=end,
            records=records,
            response_format=params.response_format,
            note="The 'video' column is a YouTube video ID — prepend https://youtube.com/watch?v= for a link.",
        )
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="youtube_analytics_demographics",
    annotations={
        "title": "Viewer Demographics (Age & Gender)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def youtube_analytics_demographics(params: DemographicsInput) -> str:
    """Get the age-group and gender breakdown of viewers, as a percentage of total watch time.

    Use this to validate or refine the channel's target-Avatar assumptions
    (e.g. confirming the audience skews toward a particular age band), or to
    check whether a specific video over/under-indexes with a segment.

    Args:
        params (DemographicsInput): Validated input containing:
            - video_id (Optional[str]): restrict to one video, or omit for channel-wide
            - start_date / end_date (Optional[str]): date range, defaults to last 28 days
            - channel_id (Optional[str]): defaults to the authenticated channel
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: A table (or JSON) with columns: ageGroup, gender, viewerPercentage
        (a float 0-100 representing % of watch time from that segment).

    Examples:
        - "What's my audience's age/gender breakdown this quarter?" -> channel-wide
        - "Does video X attract a different audience than usual?" -> video_id='X'

    Error Handling:
        - Sparse or missing rows are normal for low-traffic channels/videos — demographic data requires a minimum sample size
    """
    try:
        start, end = (
            params.start_date or _default_date_range(28)[0],
            params.end_date or _default_date_range(28)[1],
        )
        filters = f"video=={params.video_id}" if params.video_id else None
        report = await _query_reports(
            ids=_channel_filter(params.channel_id),
            start_date=start,
            end_date=end,
            metrics="viewerPercentage",
            dimensions="ageGroup,gender",
            filters=filters,
            sort="-viewerPercentage",
        )
        records = _rows_to_records(report)
        return _format_response(
            title="Viewer Demographics (Age & Gender)" + (f" — video {params.video_id}" if params.video_id else " — channel-wide"),
            start_date=start,
            end_date=end,
            records=records,
            response_format=params.response_format,
            note="viewerPercentage is share of watch time (0-100) for that age/gender segment.",
        )
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="youtube_analytics_custom_query",
    annotations={
        "title": "Custom YouTube Analytics Query (advanced)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def youtube_analytics_custom_query(params: CustomQueryInput) -> str:
    """Run a fully custom YouTube Analytics `reports.query` request — full API coverage for anything the convenience tools don't pre-package.

    Use this when you need a metric/dimension/filter combination not covered
    by the other tools — e.g. breakdowns by device type, operating system,
    country/region/city, subscription status, playback location, or
    multi-dimension cuts like 'day,deviceType'.

    Args:
        params (CustomQueryInput): Validated input containing:
            - start_date / end_date (str): YYYY-MM-DD, required
            - metrics (str): comma-separated metric names (required) — see
              https://developers.google.com/youtube/analytics/metrics
            - dimensions (Optional[str]): comma-separated dimension names — see
              https://developers.google.com/youtube/analytics/dimensions
            - filters (Optional[str]): filter expression, e.g. 'video==ID;country==US'
            - sort (Optional[str]): sort spec, e.g. '-views'
            - max_results (Optional[int]): row cap
            - channel_id (Optional[str]): defaults to the authenticated channel
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: A table (or JSON) of the raw query results, columns matching
        whatever metrics/dimensions were requested.

    Examples:
        - "Break down views by device type and operating system for May" -> metrics='views', dimensions='deviceType,operatingSystem'
        - "Show me views by country for my top video" -> metrics='views', dimensions='country', filters='video==<id>', sort='-views'

    Error Handling:
        - "Error: Bad request" -> usually an invalid metric/dimension name or an unsupported combination — double check against the API reference docs linked above
    """
    try:
        report = await _query_reports(
            ids=_channel_filter(params.channel_id),
            start_date=params.start_date,
            end_date=params.end_date,
            metrics=params.metrics,
            dimensions=params.dimensions,
            filters=params.filters,
            sort=params.sort,
            max_results=params.max_results,
        )
        records = _rows_to_records(report)
        return _format_response(
            title=f"Custom YouTube Analytics Query ({params.metrics})",
            start_date=params.start_date,
            end_date=params.end_date,
            records=records,
            response_format=params.response_format,
        )
    except Exception as e:
        return _handle_api_error(e)


if __name__ == "__main__":
    mcp.run()
