# 녹음 → 한국어 텍스트 → Notion 기록

녹음 파일(mp3, m4a, wav 등)을 한국어 텍스트로 받아쓰기(전사)하고, Notion 데이터베이스에 페이지 하나로 저장합니다.
같은 내용은 `transcripts/` 폴더에 `.md` 파일로도 남습니다.

```
녹음파일.m4a ──(Whisper 음성인식)──▶ [00:01:23] 한국어 텍스트 ──▶ Notion 페이지
```

## 1. 설치 (처음 한 번)

```bash
pip install -r requirements.txt
```

ffmpeg도 필요합니다. (Mac: `brew install ffmpeg`, Windows: `winget install ffmpeg`)

## 2. Notion 준비 (처음 한 번)

1. https://www.notion.so/my-integrations 에서 **새 통합(Integration) 만들기** → 시크릿 키(`ntn_...`) 복사 → `NOTION_TOKEN`
2. Notion에서 기록용 **데이터베이스(표)** 를 만듭니다. 권장 속성:
   - `이름` (제목) — 자동으로 `2026-10-19 파일명` 형태로 채워짐
   - `날짜` (날짜) — 녹음 파일의 날짜가 자동 입력됨 (날짜 속성이 있으면 이름은 무엇이든 상관없음)
3. 데이터베이스 페이지 오른쪽 위 `•••` → **연결(Connections)** → 1번에서 만든 통합을 추가
4. 데이터베이스 링크 `https://www.notion.so/xxxx/`**`1a2b3c...(32자리)`**`?v=...` 에서 32자리 부분 → `NOTION_DATABASE_ID`

```bash
cp .env.example .env   # 그리고 .env 에 값 입력
```

## 3. 사용법

```bash
# 기본: OPENAI_API_KEY 가 있으면 OpenAI, 없으면 내 PC(로컬)에서 전사
python transcribe_to_notion.py 회의녹음.m4a

# 제목 지정
python transcribe_to_notion.py 회의녹음.m4a --title "세제 시험 결과 회의"

# 여러 파일을 한꺼번에
python transcribe_to_notion.py 녹음/*.m4a

# Notion에 올리지 않고 텍스트 파일만 만들기
python transcribe_to_notion.py 회의녹음.m4a --no-notion
```

## 전사 방식 비교

| 방식 | 옵션 | 비용 | 속도 | 특징 |
|---|---|---|---|---|
| OpenAI API | `--backend openai` | 분당 약 $0.006 (1시간 ≈ 500원) | 빠름 | 타임스탬프 포함, 긴 파일 자동 분할 |
| OpenAI 고정밀 | `--backend openai --openai-model gpt-4o-transcribe` | 비슷 | 빠름 | 정확도↑, 타임스탬프 없음 |
| 로컬 (faster-whisper) | `--backend local` | 무료 | PC 성능에 따라 느림 | 녹음이 외부 서버로 나가지 않음 (사내 보안 자료에 적합) |

로컬이 너무 느리면 `--local-model medium` 또는 `small` 로 낮추세요 (정확도는 다소 떨어짐).

> 💡 사내 연구 회의처럼 외부 유출이 민감한 녹음은 `--backend local` 을 권장합니다.
