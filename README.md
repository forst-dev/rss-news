구글 뉴스 RSS에서 최근 24시간 경제 뉴스를 가져와 제목과 요약을 출력합니다.
GitHub Actions로 자동 실행하고, 실행 로그에서 결과를 확인할 수 있습니다.

## 로컬 실행

```bash
python scripts/fetch_economic_news.py
```

## GitHub Actions 실행

- 워크플로우 파일: `.github/workflows/economic-news.yml`
- 실행 방식:
  - 매일 UTC 00:00 (KST 09:00) 자동 실행
  - `workflow_dispatch`로 수동 실행 가능

## 로그 확인 방법

GitHub 저장소의 `Actions` 탭에서 `Economic News RSS` 워크플로우 실행 기록을 열면
스크립트 출력(뉴스 제목/요약/링크/발행일)을 로그에서 확인할 수 있습니다.