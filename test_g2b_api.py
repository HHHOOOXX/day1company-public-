"""하위호환용 진입점 — 실제 로직은 g2b_notifier 패키지로 이전됨.

GitHub Actions 워크플로우(.github/workflows/notify.yml)가
`python test_g2b_api.py notify 용역` 형태로 이 파일을 직접 호출하므로,
파일명/호출 방식을 유지하기 위해 얇은 wrapper로 남겨둔다.

사용법은 g2b_notifier/cli.py의 모듈 docstring을 참고하세요.
"""

from g2b_notifier.cli import main

if __name__ == "__main__":
    main()
