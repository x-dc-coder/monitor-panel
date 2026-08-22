#!/usr/bin/env python3
"""导出 FastAPI openapi.json（issue #6：前端 TS 类型生成管线的数据源）。

用法：python scripts/export_openapi.py [输出路径]（默认 stdout）
前端：npm run gen:types（scripts/gen_types.mjs 调用本脚本）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402


def main() -> None:
    schema = app.openapi()
    out = json.dumps(schema, ensure_ascii=False, indent=1)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(out + "\n", encoding="utf-8")
        print(f"✓ openapi.json → {sys.argv[1]} "
              f"({len(schema.get('paths', {}))} paths)", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
