#!/usr/bin/env python3
"""view.html(表示テンプレート)に、集計済みデータ(aggregate.build_data の出力)を
埋め込んで、単体で開ける HTML を書き出す。

  python3 build_view.py [入力JSON] [出力HTML]
    入力JSON  … BackupData 形式のバックアップ(既定: ../sample-data/latest.json)
    出力HTML  … 書き出し先(既定: ./dashboard.html)

Streamlit などから使うときは build_html(data) を呼べば、注入済み HTML 文字列が得られる。
"""
from __future__ import annotations
import json
import os
import sys

import aggregate

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "view.html")
# view.html 内のこの1行(データ用 script タグ)をまるごと差し替える。
MARKER_ID = '<script id="__data__">'


def build_html(backup: dict) -> str:
    """BackupData(バックアップJSON)を集計し、view.html に注入した HTML を返す。"""
    data = aggregate.build_data(backup)
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    start = html.find(MARKER_ID)
    if start == -1:
        raise RuntimeError("view.html にデータ注入用の <script id=\"__data__\"> が見つかりません")
    end = html.find("</script>", start)
    if end == -1:
        raise RuntimeError("データ注入用 script の閉じタグが見つかりません")
    end += len("</script>")
    # </ で HTML パーサが script を早期終了しないようエスケープする
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    injected = f'<script id="__data__">window.__DATA__ = {payload};</script>'
    return html[:start] + injected + html[end:]


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "sample-data", "latest.json")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "dashboard.html")
    with open(src, encoding="utf-8") as f:
        backup = json.load(f)
    html = build_html(backup)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"書き出し: {out}  ({len(html):,} バイト)")


if __name__ == "__main__":
    main()
