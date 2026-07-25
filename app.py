"""AB Workout ダッシュボードの公開用アプリ(Streamlit)。

役割はシンプルで、
  1. GitHub の非公開リポジトリからバックアップ JSON(latest.json)を取得
  2. build_view.build_html で表示用 HTML を組み立て
  3. その HTML を画面に埋め込んで表示
の3つだけ。集計や描画のロジックは持たない(aggregate.py / view.html 側の担当)。

閲覧できる人の制限(Google ログイン)は、Streamlit Community Cloud 側の
「アプリを非公開にして閲覧者をメールアドレスで指定する」機能で行う。詳しくは
同じフォルダの「デプロイ手順.md」を参照。

ローカルで動かす場合:
  cd dashboard
  streamlit run app.py
  (GitHub の設定が無いときは、自動的に ../sample-data/latest.json を表示する)
"""
from __future__ import annotations
import importlib
import json
import os

import requests
import streamlit as st
import streamlit.components.v1 as components

import aggregate
import build_view

# 同じフォルダの自作モジュールを、実行のたびに読み直す。
# 理由: view.html は build_html が毎回ファイルを開き直すので常に最新が使われるのに対し、
# aggregate.py / build_view.py は Python が一度読んだ内容をメモリに持ち続ける(sys.modules に
# 残る)。Streamlit Cloud が git push を検知してスクリプトだけ再実行した場合、
# 「新しい view.html + 古い aggregate.py」という食い違った組み合わせで動いてしまい、
# 新しい集計項目が丸ごと欠けて画面が「—」や空になる。実際に 2026-07-25 にこれが起きた。
# モジュールは定数と関数の定義しか持たないので、読み直しても副作用は無い。
for _mod in (aggregate, build_view):
    try:
        importlib.reload(_mod)
    except Exception:  # 読み直しに失敗しても、既に読めているモジュールで動かす
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "..", "sample-data", "latest.json")

st.set_page_config(page_title="AB Workout ダッシュボード", page_icon=":material/fitness_center:", layout="wide")

# Streamlit 標準の余白・ヘッダーを詰めて、ダッシュボードを画面いっぱいに見せる
st.markdown(
    """
    <style>
      header[data-testid="stHeader"] { display: none; }
      .block-container { padding: 0 !important; max-width: 100% !important; }
      [data-testid="stAppViewContainer"] { background: #0f0f10; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _github_cfg() -> dict | None:
    """GitHub 連携の設定を返す。未設定なら None(エラーではなく「まだ繋いでいない」状態)。

    必要な secrets(.streamlit/secrets.toml か Cloud の Settings → Secrets):
        [github]
        repo   = "オーナー名/リポジトリ名"   # 例: "shunya-abe/abworkout-backups"
        path   = "latest.json"               # リポジトリ内のファイルパス
        branch = "main"                      # 省略可(既定 main)
        token  = "ghp_xxx"                   # 非公開リポジトリを読める個人アクセストークン
    """
    try:
        cfg = st.secrets["github"]  # secrets ファイル自体が無いと例外になる
    except Exception:
        return None
    if not cfg.get("repo") or not cfg.get("token"):
        return None
    return dict(cfg)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_backup_from_github(cfg: dict) -> dict:
    """GitHub Contents API でバックアップ JSON を取得する。失敗時は例外を投げる。"""
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{cfg.get('path', 'latest.json')}"
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        # raw を指定すると Base64 ではなくファイルの中身そのものが返る
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {"ref": cfg.get("branch", "main")}
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    return json.loads(resp.content.decode("utf-8"))


def load_backup() -> tuple[dict, str]:
    """バックアップを読み込み、(データ, 取得元の説明) を返す。"""
    cfg = _github_cfg()
    if cfg is not None:
        try:
            return fetch_backup_from_github(cfg), "GitHub"
        except Exception as e:  # 設定はあるのに取得できない = 本当の失敗なので赤字で出す
            st.error(f"GitHub からの取得に失敗しました: {e}")
    with open(SAMPLE, encoding="utf-8") as f:
        return json.load(f), "サンプルデータ(ローカル)"


# --- 画面 ---
# 最新化はブラウザの再読み込みで行う(キャッシュは 60 秒で切れるため、リロードすればすぐ最新になる)。
# 画面上の「更新」ボタンは廃止した(リロードと役割が重複し、個人用途では不要なため)。
backup, source = load_backup()
if source != "GitHub":
    st.info(
        "現在は " + source + " を表示しています。"
        "本番の記録を出すには、GitHub 連携の設定(secrets)が必要です。"
        "詳しくは「デプロイ手順.md」を参照してください。",
        icon=":material/info:",
    )

html = build_view.build_html(backup)
# height は暫定の初期値。読み込み後に view.html 側が中身の高さへ自動調整する。
components.html(html, height=3600, scrolling=True)
