#!/usr/bin/env python3
"""Render reviewed report JSON into a deterministic, phone-readable long PNG."""
import argparse
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from report_tools import dt

ROOT = Path(__file__).resolve().parents[1]
LABELS = {"self_activity": "本人活动", "conversation": "多人对话", "media": "疑似媒体",
          "inference": "AI 推断", "unknown": "归属未知"}
CONFIDENCE = {"high": "高", "medium": "中", "low": "低"}
STATUS = {"observed": "记录可见", "discussed": "仅讨论", "proposed": "提议",
          "confirmed": "已确认", "completed": "已完成", "suggested": "建议", "unknown": "未确认"}


def validate(data, expected_project_id=None):
    if not isinstance(data.get("project_id"), str) or not data["project_id"].strip():
        raise ValueError("A project ID is required")
    if expected_project_id is not None and data["project_id"] != expected_project_id:
        raise ValueError("Unexpected project")
    if data.get("privacy_reviewed") is not True:
        raise ValueError("Agent must review and minimize sensitive data before rendering")
    a, b = dt(data["window"]["start"]), dt(data["window"]["end_exclusive"])
    if b <= a:
        raise ValueError("Invalid window")
    ZoneInfo(data["timezone"])
    if data["retrieval"]["status"] not in ("complete", "partial", "no_data", "metadata_only"):
        raise ValueError("Unknown retrieval status")
    evidence = {e["id"]: e for e in data["evidence"]}
    if len(evidence) != len(data["evidence"]):
        raise ValueError("Duplicate evidence labels")
    for e in evidence.values():
        if e["source_table"] not in ("transcripts", "recordings", "auday_insights", "memories"):
            raise ValueError("Unsupported evidence source")
        if not e.get("source_id") or not e.get("paraphrase"):
            raise ValueError("Evidence needs source ID and minimized paraphrase")
        es, ee = dt(e["start"]), dt(e["end"])
        if es >= b or (ee <= a and es < a) or ee < es:
            raise ValueError("Evidence outside report window")
    previous = a
    for event in data["events"]:
        start, end = dt(event["start"]), dt(event["end"])
        if start < previous or start < a or end > b or end < start:
            raise ValueError("Timeline must be ordered inside the report window")
        previous = start
        if event["category"] not in LABELS or event["confidence"] not in CONFIDENCE or event["status"] not in STATUS:
            raise ValueError("Unsupported attribution, confidence or status")
        if not isinstance(event["speaker"], str) or not event["speaker"].strip():
            raise ValueError("Use speaker=unknown when attribution is unavailable")
        if event["category"] == "self_activity" and (
                event["speaker"] == "unknown" or not event.get("speaker_basis")):
            raise ValueError("Personal activity requires confirmed speaker attribution and its basis")
        if event["category"] == "inference" and event["confidence"] != "low":
            raise ValueError("Inference must remain low confidence")
        if event["category"] in ("media", "unknown", "inference") and event["status"] in ("completed", "confirmed"):
            raise ValueError("Ambiguous or media content cannot confirm personal completion")
        refs = event["evidence_ids"]
        if not refs or any(ref not in evidence for ref in refs):
            raise ValueError("Every event needs resolvable evidence")
        if not any(dt(evidence[ref]["start"]) <= end and dt(evidence[ref]["end"]) >= start for ref in refs):
            raise ValueError("Event needs temporally relevant evidence")
    for key in ("highlights", "ideas", "todos"):
        for item in data.get(key, []):
            if not item.get("evidence_ids") or any(x not in evidence for x in item["evidence_ids"]):
                raise ValueError("Highlights/ideas/todos must reference evidence")
    text = json.dumps(data, ensure_ascii=False)
    if re.search(r"-----BEGIN .*PRIVATE KEY|eyJ[A-Za-z0-9_-]{15,}\.|sb_secret_[A-Za-z0-9]+", text):
        raise ValueError("Possible credential: remove it before export")


def find_font(explicit=None):
    paths = [explicit, os.environ.get("PERSONAL_REPORT_FONT"),
             "/System/Library/Fonts/PingFang.ttc",
             "/System/Library/Fonts/Hiragino Sans GB.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
             "C:/Windows/Fonts/msyh.ttc"]
    for path in paths:
        if path and Path(path).is_file():
            return path
    raise RuntimeError("No Chinese font found. Supply a local CJK font with --font.")


class Canvas:
    def __init__(self, theme, font):
        self.theme, self.font_path = theme, font
        self.width = int(theme["width"])
        if self.width != 1080:
            raise ValueError("This layout uses a 1080px canvas")
        self.image = Image.new("RGB", (self.width, 4000), theme["background"])
        self.draw = ImageDraw.Draw(self.image)
        self.y = 0
        self.fonts = {}

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = ImageFont.truetype(self.font_path, size)
        return self.fonts[size]

    def ensure(self, bottom):
        if bottom > self.image.height:
            larger = Image.new("RGB", (self.width, max(bottom + 200, self.image.height * 2)),
                               self.theme["background"])
            larger.paste(self.image, (0, 0))
            self.image = larger
            self.draw = ImageDraw.Draw(larger)

    def box(self, bounds, fill, outline=None, radius=20):
        self.ensure(int(bounds[3]) + 2)
        self.draw.rounded_rectangle(bounds, radius, fill, outline)

    def lines(self, text, size, width):
        lines = []
        for paragraph in str(text).split("\n"):
            line = ""
            for char in paragraph:
                if line and self.draw.textlength(line + char, font=self.font(size)) > width:
                    lines.append(line)
                    line = char
                else:
                    line += char
            lines.append(line)
        return lines

    def text(self, text, x, y, size=30, color=None, width=948):
        lines = self.lines(text, size, width)
        height = len(lines) * (size + 15)
        self.ensure(int(y + height + 12))
        for i, line in enumerate(lines):
            self.draw.text((x, y + i * (size + 15)), line, font=self.font(size),
                           fill=color or self.theme["ink"])
        return height

    def section(self, number, title, english):
        self.y += 45
        self.ensure(self.y + 120)
        self.draw.line((60, self.y, 1020, self.y), fill=self.theme["line"], width=2)
        self.y += 43
        self.text(f"{number:02}", 60, self.y + 5, 24, self.theme["blue"])
        self.text(title, 119, self.y, 37)
        self.text(english, 780, self.y + 10, 19, self.theme["muted"], 250)
        self.y += 85


def markdown(data):
    rows = [f"# {data['title']}", "", data["date_label"], "", data["summary"], "",
            f"时区：{data['timezone']}；读取状态：{data['retrieval']['status']}", ""]
    for e in data["events"]:
        rows += [f"- {e['display_time']}｜{e['title']}（{LABELS[e['category']]}；说话人：{e['speaker']}；"
                 f"置信度：{CONFIDENCE[e['confidence']]}；{STATUS[e['status']]}）",
                 f"  {e['description']} [{' / '.join(e['evidence_ids'])}]"]
    for key, title in (("highlights", "重点事件"), ("ideas", "创意与决策"), ("todos", "待办")):
        rows += ["", f"## {title}", ""]
        rows += [f"- {x['title']}：{x['description']} [{' / '.join(x['evidence_ids'])}]"
                 for x in data.get(key, [])] or ["- 无可核实内容"]
    rows += ["", "## 覆盖与限制", ""]
    rows += [f"- {x}" for x in data["limitations"]]
    rows += ["", "## 证据索引（均为脱敏转述）", ""]
    for e in data["evidence"]:
        rows += [f"- {e['id']}｜{e['source_table']}:{e['source_id']}｜录音 {e.get('recording_id','不适用')}｜"
                 f"{e['start']} — {e['end']}｜{e['paraphrase']}"]
    return "\n".join(rows) + "\n"


def render(data, output, font=None, pdf=False, expected_project_id=None):
    validate(data, expected_project_id)
    theme = json.loads((ROOT / "assets/theme.json").read_text(encoding="utf-8"))
    c = Canvas(theme, find_font(font))
    # A restrained lavender wash echoes the reference without external image services.
    for y in range(670):
        c.draw.line((0, y, 1080, y), fill=(248 - y // 170, 250 - y // 170, 255))
    c.draw.rectangle((0, 0, 1080, 12), fill=theme["blue"])
    c.text("PERSONAL ACTIVITY REPORT", 60, 70, 25, theme["blue"])
    c.box((889, 59, 1018, 112), theme["purple"], radius=26)
    c.text(data.get("badge", "日报"), 922, 65, 26, "#FFFFFF", 90)
    c.text(data["title"], 60, 151, 64, width=950)
    c.y = 251 + c.text(data["subtitle"], 60, 251, 27, theme["muted"])
    c.y += 28
    label_height = len(c.lines(data["date_label"], 28, 870)) * 43
    c.box((60, c.y, 1020, c.y + label_height + 45), "#FFFFFF", theme["line"])
    c.text(data["date_label"], 88, c.y + 18, 28, width=885)
    c.y += label_height + 75
    summary_h = len(c.lines(data["summary"], 31, 890)) * 46
    c.box((60, c.y, 68, c.y + summary_h), theme["purple"], radius=0)
    c.text(data["summary"], 91, c.y - 4, 31, width=890)
    c.y += summary_h + 32

    c.section(1, "活动概览", "OVERVIEW")
    metrics = data["metrics"]
    for i, metric in enumerate(metrics):
        if i % 2 == 0 and i:
            c.y += 165
        x = 60 + (i % 2) * 490
        c.box((x, c.y, x + 470, c.y + 145), theme["card"], theme["line"])
        c.text(metric["value"], x + 24, c.y + 9, 43,
               theme["teal"] if i % 2 == 0 else theme["purple"], 420)
        c.text(metric["label"], x + 24, c.y + 81, 24, theme["muted"], 420)
    if metrics:
        c.y += 162
    c.text(data["coverage_note"], 60, c.y + 7, 25, theme["muted"])
    c.y += len(c.lines(data["coverage_note"], 25, 948)) * 40 + 8

    c.section(2, "活动时间线", "TIMELINE")
    for i, event in enumerate(data["events"], 1):
        start_y = c.y
        color = theme["purple"] if event["category"] == "media" else theme["blue"]
        c.box((60, c.y + 3, 110, c.y + 53), color, radius=12)
        c.text(str(i), 75, c.y + 3, 26, "#FFFFFF", 40)
        c.y += c.text(event["display_time"], 143, c.y - 3, 25, color, 850) + 4
        c.y += c.text(event["title"], 143, c.y, 34, width=850) + 6
        c.y += c.text(event["description"], 143, c.y, 29, width=850) + 9
        tag = (f"{LABELS[event['category']]} · 说话人"
               f"{'未知' if event['speaker'] == 'unknown' else event['speaker']} · "
               f"{STATUS[event['status']]} · 置信{CONFIDENCE[event['confidence']]}")
        c.y += c.text(tag, 143, c.y, 23, theme["muted"], 850)
        c.y += c.text("证据 " + " / ".join(event["evidence_ids"]), 143, c.y, 22, color, 850) + 30
        c.draw.line((85, start_y + 65, 85, c.y - 12), fill=theme["line"], width=3)
    if not data["events"]:
        c.y += c.text("该窗口没有可核实的活动内容。", 60, c.y, 30) + 10

    for num, key, title, english in (
            (3, "highlights", "重点事件", "HIGHLIGHTS"),
            (4, "ideas", "创意与决策", "IDEAS"),
            (5, "todos", "待办与待确认", "FOLLOW UP")):
        c.section(num, title, english)
        items = data.get(key, [])
        if not items:
            c.y += c.text("暂无可核实内容。", 60, c.y, 29, theme["muted"])
        for item in items:
            title_h = len(c.lines(item["title"], 32, 900)) * 47
            desc_h = len(c.lines(item["description"], 28, 900)) * 43
            ref_h = len(c.lines("证据 " + " / ".join(item["evidence_ids"]), 22, 900)) * 37
            height = title_h + desc_h + ref_h + 54
            c.box((60, c.y, 1020, c.y + height), theme["card"], theme["line"])
            pos = c.y + 20
            pos += c.text(item["title"], 85, pos, 32, width=900) + 5
            pos += c.text(item["description"], 85, pos, 28, width=900) + 8
            c.text("证据 " + " / ".join(item["evidence_ids"]), 85, pos, 22, theme["blue"], 900)
            c.y += height + 20

    c.section(6, "证据与可信度", "EVIDENCE")
    for item in data["limitations"]:
        c.y += c.text("• " + item, 60, c.y, 27, width=950) + 17
    c.y += 14
    for e in data["evidence"]:
        c.y += c.text(f"{e['id']}  {e['paraphrase']}", 60, c.y, 24, width=950)
        short = e["source_id"][:8]
        c.y += c.text(f"{e['source_table']} / {short}… · 完整 ID 见证据 JSON", 60, c.y, 21,
                     theme["muted"], 950) + 18
    c.y += 28
    c.y += c.text("PERSONAL REPORT  /  只读生成 · 默认脱敏", 60, c.y, 22, theme["blue"])
    c.y += c.text(f"查询于 {data['retrieval']['queried_at']} · {data['timezone']}",
                 60, c.y, 20, theme["muted"]) + 45
    result = c.image.crop((0, 0, 1080, c.y))
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    result.save(target, format="PNG")
    target.with_suffix(".md").write_text(markdown(data), encoding="utf-8")
    if pdf:
        result.save(target.with_suffix(".pdf"), format="PDF", resolution=144)
    return result.size


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--font")
    parser.add_argument("--pdf", action="store_true")
    parser.add_argument("--project-id", help="Expected authorized project ID; use for live reports")
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(json.dumps({"pixels": render(data, args.output, args.font, args.pdf, args.project_id),
                      "output": args.output}, ensure_ascii=False))


if __name__ == "__main__":
    main()
