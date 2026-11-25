"""PC에서 중요한 글을 직접 넣기.

사용법 (보통은 '글추가.bat' 더블클릭으로 실행됩니다)
  python scripts/add.py 파일.txt
  python scripts/add.py "https://www.hankyung.com/article/..."

파일 첫 줄에 '출처: KB 채권' 처럼 쓰면 출처로 기록됩니다.
'메모:' 로 시작하면 내 생각 메모로 저장됩니다.
"""
from __future__ import annotations

import sys
from pathlib import Path

from collect import make_item, save_item


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    arg = sys.argv[1]
    p = Path(arg)
    text = p.read_text(encoding="utf-8-sig") if p.exists() else " ".join(sys.argv[1:])
    source = "직접 입력"
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("출처:"):
        source = lines[0].split(":", 1)[1].strip() or source
        text = "\n".join(lines[1:])
    if not text.strip():
        print("내용이 비어 있어 저장하지 않았습니다.")
        return
    item = make_item(text, source)
    save_item(item)
    print(f"✅ 저장: [{item['category']}] {item['title']}")
    for b in item["bullets"]:
        print(f"   - {b}")


if __name__ == "__main__":
    main()
