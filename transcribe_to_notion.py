#!/usr/bin/env python3
"""녹음 파일을 한국어 텍스트로 변환(전사)하고 Notion 데이터베이스에 페이지로 저장한다.

사용 예:
    python transcribe_to_notion.py 회의녹음.m4a
    python transcribe_to_notion.py a.mp3 b.m4a --backend local --title "주간 회의"

필요한 환경변수(.env 파일도 가능):
    NOTION_TOKEN        Notion 내부 통합(Integration) 시크릿
    NOTION_DATABASE_ID  기록을 쌓을 Notion 데이터베이스 ID
    OPENAI_API_KEY      --backend openai 일 때만 필요
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_TEXT_LIMIT = 2000  # rich_text 하나에 들어갈 수 있는 최대 글자 수
NOTION_BLOCKS_PER_REQUEST = 100
OPENAI_FILE_LIMIT = 24 * 1024 * 1024  # OpenAI 업로드 한도(25MB)보다 약간 작게
CHUNK_SECONDS = 600  # 큰 파일은 10분 단위로 잘라서 전송


@dataclass
class Segment:
    start: float
    end: float
    text: str


def load_dotenv(path: Path = Path(".env")) -> None:
    """python-dotenv 없이 간단한 KEY=VALUE 형식의 .env를 읽는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def audio_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


# ---------------------------------------------------------------- 전사(STT)

def transcribe_local(path: Path, model_size: str) -> list[Segment]:
    """faster-whisper로 내 컴퓨터에서 직접 전사한다(무료, 파일이 외부로 나가지 않음)."""
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(path), language="ko", vad_filter=True, beam_size=5)
    result = []
    for seg in segments:
        print(f"  [{fmt_time(seg.start)}] {seg.text.strip()}", flush=True)
        result.append(Segment(seg.start, seg.end, seg.text.strip()))
    return result


def split_audio(path: Path, workdir: Path) -> list[tuple[Path, float]]:
    """OpenAI 업로드 한도를 넘지 않도록 ffmpeg로 mp3 조각을 만든다. (조각 경로, 시작 시각) 목록."""
    pattern = workdir / "chunk_%03d.mp3"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
         "-b:a", "64k", "-f", "segment", "-segment_time", str(CHUNK_SECONDS), str(pattern)],
        check=True,
    )
    return [(p, i * CHUNK_SECONDS) for i, p in enumerate(sorted(workdir.glob("chunk_*.mp3")))]


def transcribe_openai(path: Path, model: str) -> list[Segment]:
    """OpenAI 음성 인식 API로 전사한다(빠르고 정확, 유료)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY 가 설정되어 있지 않습니다. (.env 확인 또는 --backend local 사용)")

    with tempfile.TemporaryDirectory() as tmp:
        if path.stat().st_size <= OPENAI_FILE_LIMIT and (audio_duration(path) or 0) <= CHUNK_SECONDS * 2:
            chunks = [(path, 0.0)]
        else:
            chunks = split_audio(path, Path(tmp))

        segments: list[Segment] = []
        for i, (chunk, offset) in enumerate(chunks, 1):
            print(f"  조각 {i}/{len(chunks)} 전송 중...", flush=True)
            # whisper-1만 구간별 타임스탬프(verbose_json)를 지원한다.
            fmt = "verbose_json" if model == "whisper-1" else "json"
            with chunk.open("rb") as f:
                resp = requests.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": model, "language": "ko", "response_format": fmt},
                    files={"file": (chunk.name, f)},
                    timeout=600,
                )
            if resp.status_code != 200:
                sys.exit(f"OpenAI 전사 실패 ({resp.status_code}): {resp.text}")
            body = resp.json()
            if body.get("segments"):
                for s in body["segments"]:
                    segments.append(Segment(s["start"] + offset, s["end"] + offset, s["text"].strip()))
            else:
                segments.append(Segment(offset, offset, body.get("text", "").strip()))
    return segments


# ---------------------------------------------------------------- Notion

def notion_headers() -> dict[str, str]:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("NOTION_TOKEN 이 설정되어 있지 않습니다. README의 'Notion 준비' 절을 참고하세요.")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method: str, url: str, payload: dict | None = None) -> dict:
    resp = requests.request(method, url, headers=notion_headers(), data=json.dumps(payload) if payload else None, timeout=60)
    if resp.status_code >= 300:
        sys.exit(f"Notion API 오류 ({resp.status_code}): {resp.text}")
    return resp.json()


def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def segments_to_blocks(segments: list[Segment], timestamps: bool) -> list[dict]:
    """구간들을 2000자 이하 문단 블록으로 묶는다."""
    blocks, buf = [], ""
    for seg in segments:
        if not seg.text:
            continue
        line = f"[{fmt_time(seg.start)}] {seg.text}" if timestamps else seg.text
        # 한 구간이 한도를 넘는 경우 잘라서 넣는다.
        while len(line) > NOTION_TEXT_LIMIT:
            if buf:
                blocks.append(paragraph(buf))
                buf = ""
            blocks.append(paragraph(line[:NOTION_TEXT_LIMIT]))
            line = line[NOTION_TEXT_LIMIT:]
        sep = "\n" if timestamps else " "
        if buf and len(buf) + len(sep) + len(line) > NOTION_TEXT_LIMIT:
            blocks.append(paragraph(buf))
            buf = ""
        buf = f"{buf}{sep}{line}" if buf else line
    if buf:
        blocks.append(paragraph(buf))
    return blocks


def save_to_notion(title: str, recorded: dt.date, source: str, segments: list[Segment]) -> str:
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not database_id:
        sys.exit("NOTION_DATABASE_ID 가 설정되어 있지 않습니다.")

    schema = notion_request("GET", f"{NOTION_API}/databases/{database_id}")["properties"]
    title_prop = next(name for name, p in schema.items() if p["type"] == "title")
    properties: dict = {title_prop: {"title": [{"text": {"content": title}}]}}
    # 데이터베이스에 날짜 속성이 있으면 녹음 날짜를 채운다.
    date_prop = next((name for name, p in schema.items() if p["type"] == "date"), None)
    if date_prop:
        properties[date_prop] = {"date": {"start": recorded.isoformat()}}

    has_timestamps = any(s.end > s.start for s in segments)
    children = [
        {"object": "block", "type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "🎙️"},
            "rich_text": [{"type": "text", "text": {"content": f"원본 파일: {source}"}}],
        }},
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": [{"type": "text", "text": {"content": "전사 내용"}}]}},
        *segments_to_blocks(segments, has_timestamps),
    ]

    page = notion_request("POST", f"{NOTION_API}/pages", {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": children[:NOTION_BLOCKS_PER_REQUEST],
    })
    # 한 번에 100블록까지만 넣을 수 있으므로 나머지는 이어 붙인다.
    for i in range(NOTION_BLOCKS_PER_REQUEST, len(children), NOTION_BLOCKS_PER_REQUEST):
        notion_request("PATCH", f"{NOTION_API}/blocks/{page['id']}/children",
                       {"children": children[i:i + NOTION_BLOCKS_PER_REQUEST]})
    return page["url"]


# ---------------------------------------------------------------- main

def save_markdown(outdir: Path, title: str, segments: list[Segment]) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{title}.md"
    lines = [f"# {title}", ""]
    lines += [f"[{fmt_time(s.start)}] {s.text}" if s.end > s.start else s.text for s in segments if s.text]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="녹음 파일 → 한국어 전사 → Notion 저장")
    parser.add_argument("files", nargs="+", type=Path, help="녹음 파일(mp3, m4a, wav 등)")
    parser.add_argument("--backend", choices=["openai", "local"],
                        default="openai" if os.environ.get("OPENAI_API_KEY") else "local",
                        help="openai: API 사용(빠름, 유료) / local: 내 PC에서 faster-whisper(무료)")
    parser.add_argument("--openai-model", default="whisper-1",
                        help="whisper-1(타임스탬프 있음) 또는 gpt-4o-transcribe(더 정확, 타임스탬프 없음)")
    parser.add_argument("--local-model", default="large-v3",
                        help="faster-whisper 모델 크기 (small, medium, large-v3 등)")
    parser.add_argument("--title", help="Notion 페이지 제목 (기본: 날짜 + 파일명)")
    parser.add_argument("--outdir", type=Path, default=Path("transcripts"), help="로컬 사본(.md) 저장 폴더")
    parser.add_argument("--no-notion", action="store_true", help="Notion에 올리지 않고 로컬 파일만 저장")
    args = parser.parse_args()

    for path in args.files:
        if not path.exists():
            sys.exit(f"파일을 찾을 수 없습니다: {path}")

        recorded = dt.date.fromtimestamp(path.stat().st_mtime)
        title = args.title or f"{recorded.isoformat()} {path.stem}"
        if args.title and len(args.files) > 1:
            title = f"{args.title} - {path.stem}"

        print(f"▶ {path.name} 전사 중 ({args.backend})...", flush=True)
        if args.backend == "openai":
            segments = transcribe_openai(path, args.openai_model)
        else:
            segments = transcribe_local(path, args.local_model)

        md = save_markdown(args.outdir, title, segments)
        print(f"  로컬 저장: {md}")
        if not args.no_notion:
            url = save_to_notion(title, recorded, path.name, segments)
            print(f"  Notion 저장 완료: {url}")


if __name__ == "__main__":
    main()
