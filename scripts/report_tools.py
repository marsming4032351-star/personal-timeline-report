#!/usr/bin/env python3
"""Local date/SELECT planning and coverage helpers. Never connects to a database."""
import argparse
import json
import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

def dt(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A timezone-aware timestamp is required")
    return result


def window(period, tz="Asia/Shanghai", now=None, start=None, end=None, hours=None):
    zone = ZoneInfo(tz)
    current = (dt(now) if now else datetime.now(timezone.utc)).astimezone(zone)
    midnight = datetime.combine(current.date(), time(), zone)
    if period == "yesterday":
        a, b = midnight - timedelta(days=1), midnight
    elif period == "today":
        a, b = midnight, current
    elif period == "last-week":
        b = midnight - timedelta(days=current.weekday())
        a = b - timedelta(days=7)
    elif period == "last-7-days":
        a, b = (current.astimezone(timezone.utc) - timedelta(hours=168)).astimezone(zone), current
    elif period == "hours":
        if hours is None or hours <= 0:
            raise ValueError("hours must be positive")
        a, b = (current.astimezone(timezone.utc) - timedelta(hours=hours)).astimezone(zone), current
    elif period == "custom":
        a = datetime.combine(datetime.strptime(start, "%Y-%m-%d").date(), time(), zone)
        b = datetime.combine(datetime.strptime(end or start, "%Y-%m-%d").date(), time(), zone)
        b += timedelta(days=1)
    else:
        raise ValueError("Unknown period")
    if b <= a:
        raise ValueError("Empty or reversed date window")
    return {"timezone": tz, "start": a.isoformat(), "end_exclusive": b.isoformat(),
            "start_utc": a.astimezone(timezone.utc).isoformat(),
            "end_utc": b.astimezone(timezone.utc).isoformat(),
            "resolved_now": current.isoformat()}


def sql(kind, start, end, limit=200, offset=0, owner=None, single_user=False):
    a, b = dt(start), dt(end)
    if b <= a or not 1 <= limit <= 200 or offset < 0:
        raise ValueError("Invalid window or pagination")
    lo, hi = f"'{a.isoformat()}'::timestamptz", f"'{b.isoformat()}'::timestamptz"
    if kind == "metadata":
        return ("SELECT table_name,column_name,data_type,is_nullable "
                "FROM information_schema.columns WHERE table_schema='public' AND "
                "table_name IN ('recordings','transcripts','auday_insights','sync_runs','memories') "
                "ORDER BY table_name,ordinal_position;")
    if kind == "owners":
        return ("SELECT 'recordings' AS source,owner_id,count(*) FROM public.recordings "
                "WHERE deleted_at IS NULL GROUP BY owner_id UNION ALL "
                "SELECT 'transcripts',owner_id,count(*) FROM public.transcripts "
                "WHERE deleted_at IS NULL GROUP BY owner_id UNION ALL "
                "SELECT 'auday_insights',owner_id,count(*) FROM public.auday_insights "
                "WHERE deleted_at IS NULL GROUP BY owner_id;")
    if not owner and not single_user:
        raise ValueError("Confirm single-user project or supply confirmed owner UUID")
    owner_filter = f" AND owner_id='{uuid.UUID(owner)}'::uuid" if owner else ""
    # Half-open overlap includes recordings crossing midnight; upload time never filters activity.
    predicates = {
        "recordings": f"deleted_at IS NULL{owner_filter} AND started_at < {hi} AND "
                      f"(COALESCE(ended_at,started_at + COALESCE(duration_sec,0)*interval '1 second') > {lo} "
                      f"OR started_at >= {lo})",
        "transcripts": f"deleted_at IS NULL{owner_filter} AND started_at < {hi} AND "
                       f"(COALESCE(ended_at,started_at) > {lo} OR started_at >= {lo})",
        "auday_insights": f"deleted_at IS NULL{owner_filter} AND started_at < {hi} AND "
                          f"(COALESCE(ended_at,started_at) > {lo} OR started_at >= {lo})",
        "memories": f"deleted_at IS NULL AND occurred_at >= {lo} AND occurred_at < {hi}",
    }
    # memories and sync_runs have no owner_id: never include them for tenant-filtered access.
    allowed = list(predicates) if single_user and not owner else list(predicates)[:3]
    if kind == "counts":
        parts = [f"SELECT '{k}' AS source,count(*) AS total FROM public.{k} WHERE {predicates[k]}"
                 for k in allowed]
        parts.extend(
            f"SELECT '{k}_missing_time',count(*) FROM public.{k} "
            f"WHERE deleted_at IS NULL AND started_at IS NULL{owner_filter}"
            for k in ("transcripts", "auday_insights"))
        if "memories" in allowed:
            parts.append("SELECT 'memories_missing_time',count(*) FROM public.memories "
                         "WHERE deleted_at IS NULL AND occurred_at IS NULL")
        return " UNION ALL ".join(parts) + ";"
    if kind == "sync_runs":
        if owner or not single_user:
            raise ValueError("sync_runs requires confirmed single-user project")
        return ("SELECT id,started_at,finished_at,mode,status,counts,verified "
                f"FROM public.sync_runs ORDER BY started_at DESC,id DESC LIMIT {limit} OFFSET {offset};")
    if kind not in allowed:
        raise ValueError("Unknown or unscoped table")
    columns = {
        "recordings": "id,external_id,owner_id,session_id,device,source,started_at,ended_at,duration_sec,synced_at",
        "transcripts": "id,recording_id,owner_id,external_id,session_id,started_at,ended_at,text,segments,audio_asset_ids,state,synced_at",
        "auday_insights": "id,external_id,owner_id,kind,state,title,text,started_at,ended_at,source_ids,synced_at",
        "memories": "id,kind,title,content,occurred_at,occurred_tz,source,source_ref,tags",
    }
    order = "occurred_at" if kind == "memories" else "started_at"
    return (f"SELECT {columns[kind]} FROM public.{kind} WHERE {predicates[kind]} "
            f"ORDER BY {order},id LIMIT {limit} OFFSET {offset};")


def union_seconds(intervals):
    merged = []
    for a, b in sorted(intervals):
        if b < a:
            raise ValueError("Reversed interval")
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return sum((b - a).total_seconds() for a, b in merged)


def coverage(recordings, transcripts, start, end):
    a, b = dt(start), dt(end)
    linked = {t["recording_id"] for t in transcripts if t.get("recording_id")}
    assets = {asset for t in transcripts for asset in (t.get("audio_asset_ids") or [])}
    intervals, gaps, asset_links = [], [], []
    for r in recordings:
        begin = dt(r["started_at"])
        finish = dt(r["ended_at"]) if r.get("ended_at") else begin + timedelta(seconds=r.get("duration_sec") or 0)
        if finish < begin:
            raise ValueError("Invalid recording interval")
        if begin >= b or (finish <= a and begin < a):
            continue
        intervals.append((max(a, begin), min(b, finish)))
        if r["id"] not in linked:
            if r.get("external_id") in assets:
                asset_links.append(r["id"])
            else:
                gaps.append({"recording_id": r["id"], "start": max(a, begin).isoformat(),
                             "end": min(b, finish).isoformat()})
    return {"recording_union_seconds": union_seconds(intervals),
            "recordings_without_linked_transcripts": gaps,
            "asset_linked_recordings": asset_links,
            "unlinked_transcript_ids": [t["id"] for t in transcripts
                                      if not t.get("recording_id") and not t.get("audio_asset_ids")],
            "note": "Linked transcript presence is not full transcription or personal-activity coverage."}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    win = sub.add_parser("window")
    win.add_argument("--period", choices=["yesterday", "today", "last-week", "last-7-days", "hours", "custom"], required=True)
    win.add_argument("--tz", default="Asia/Shanghai")
    win.add_argument("--now")
    win.add_argument("--start")
    win.add_argument("--end")
    win.add_argument("--hours", type=float)
    query = sub.add_parser("sql")
    query.add_argument("--kind", choices=["metadata", "owners", "counts", "recordings", "transcripts", "auday_insights", "memories", "sync_runs"], required=True)
    query.add_argument("--start", required=True)
    query.add_argument("--end", required=True)
    query.add_argument("--limit", type=int, default=200)
    query.add_argument("--offset", type=int, default=0)
    query.add_argument("--owner")
    query.add_argument("--single-user", action="store_true")
    cov = sub.add_parser("coverage")
    cov.add_argument("--input", required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "window":
        print(json.dumps(window(**args), ensure_ascii=False, indent=2))
    elif command == "sql":
        print(sql(**args))
    else:
        with open(args["input"], encoding="utf-8") as handle:
            data = json.load(handle)
        print(json.dumps(coverage(data["recordings"], data["transcripts"],
                                  data["start"], data["end"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
