"""AB Workout のバックアップ JSON(BackupData 形式)を読み、ダッシュボードの各グラフが
必要とする数値に集計する。ここは「JSON → 表示用データ」の変換に徹し、描画やネットワークは持たない
(テストしやすく、データ入手経路に依存しないため)。

用語の補足:
- 推定1RM(すいていワンアールエム): 1回だけ挙げられる最大重量の推定値。ここでは Epley 式
  e1RM = 重量 × (1 + 回数 / 30) で計算する。実際に限界まで挙げなくても筋力の目安になる。
- WORK セット: 本番セット。ウォームアップ(WARMUP)は集計から除く(総量が水増しされるため)。
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import date, timedelta

N_WEEKS = 52  # 常に直近12ヶ月を対象にする
LB_TO_KG = 0.45359237  # ポンド → キログラム

# BodyPart(DBの名前) → ダッシュボードの部位キー・日本語ラベル・系列番号(色)
# CARDIO(有酸素)は「部位別セット数」の積み上げには含めない(重量×回数の概念が薄いため)。
PART_ORDER = ["chest", "back", "legs", "sh", "arms", "core"]
BODYPART_MAP = {
    "CHEST":     ("chest", "胸",   1),
    "BACK":      ("back",  "背中", 2),
    "LEGS":      ("legs",  "脚",   3),
    "SHOULDERS": ("sh",    "肩",   4),
    "ARMS":      ("arms",  "腕",   5),
    "CORE":      ("core",  "体幹", 6),
}
PART_LABEL = {k: v[1] for k, v in BODYPART_MAP.items()}
PART_CLS = {v[0]: v[2] for v in BODYPART_MAP.values()}

# BIG3(ベンチ・スクワット・デッドリフト)。実データの種目名に合わせて「名前に含む語」で拾う
# (バーベル優先)。「成長の証拠」セクションの推定1RM合計カードで使う。
BIG3 = [
    ("bench", "ベンチプレス", ["バーベルベンチプレス", "ベンチプレス"]),
    ("squat", "スクワット",   ["バーベルスクワット", "スクワット"]),
    ("dead",  "デッドリフト", ["デッドリフト(バーベル)", "デッドリフト"]),
]


def _pdate(s: str) -> date:
    return date.fromisoformat(s)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def epley_e1rm(weight_kg: float, reps: int) -> float:
    return round(weight_kg * (1 + reps / 30) * 10) / 10


def set_volume_kg(s: dict, e: dict) -> float:
    """1セットが総挙上量に寄与する重さ(kg)を返す。

    - WEIGHT_REPS(重量×回数の種目): 重量 × 回数。
    - BODYWEIGHT_REPS(自重種目): 「追加重量」× 回数のみ。バックアップの weightKg には体重が
      含まれており(懸垂 41.9kg = 体重 61.9 − アシスト 20 など)、そのまま足すと腹筋22回が
      バーベル種目より重く見えてしまうため、外部負荷ぶんだけを数える。
      アシスト(マイナスの追加重量)は 0 として扱う(挙上量がマイナスになるのを防ぐため)。
    - 時間/距離種目: 0(重量の概念が無い)。
    """
    reps = s.get("reps")
    if not reps:
        return 0.0
    rt = e.get("recordType")
    if rt == "WEIGHT_REPS":
        wkg = s.get("weightKg")
        return wkg * reps if wkg is not None else 0.0
    if rt == "BODYWEIGHT_REPS":
        extra = s.get("weight")
        if extra is None:
            return 0.0
        if s.get("unit") == "LB":
            extra *= LB_TO_KG
        return max(0.0, extra) * reps
    return 0.0


def _with_is_first(pr: dict, first_seen: dict) -> list[dict]:
    """PR記録(ex_id -> best set)に、その種目を初めて記録した日と同じ日付かどうか(isFirst)を
    付けて一覧に変換する。初めて記録した日がそのまま自己ベストとして並ぶ行(=まだ更新して
    いない)と、本当に前回を上回った行を画面側で見分けられるようにするため。"""
    out = []
    for ex_id, p in pr.items():
        p = dict(p)
        p["isFirst"] = p["date"] == first_seen.get(ex_id)
        out.append(p)
    return out


def _merge_best(cells: dict, key, wkg: float, reps: int, e1: float) -> None:
    """cells[key](週または日)に1セットを反映する。top/reps は最重量のセット(同重量なら
    回数の多いほう)、e1rm はその期間の全セットでの最大値。"""
    cur = cells.get(key)
    if cur is None:
        cells[key] = {"top": wkg, "reps": reps, "e1rm": e1}
        return
    if wkg > cur["top"] or (wkg == cur["top"] and reps > cur["reps"]):
        cur["top"], cur["reps"] = wkg, reps
    cur["e1rm"] = max(cur["e1rm"], e1)


# --- 種目ごとの伸び・停滞の一覧表 ---
PROGRESS_WINDOW_WEEKS = 4      # 「直近4週」(今週を含む)。停滞の判定と判断材料に使う
PROGRESS_PICK_WEEKS = 8        # 一覧表に載せる種目: 直近8週に…
PROGRESS_PICK_MIN_DAYS = 2     # …2日以上実施した重量×回数の種目
PROGRESS_DOWN_RATIO = 0.95     # 直近4週の最高が自己ベストのこの割合を下回ったら「下降」
PROGRESS_SPARK_WEEKS = 12      # 小さな折れ線に使う週数
PROGRESS_ORDER = {"down": 0, "flat": 1, "up": 2, "new": 3}


def _progress_rows(bk: "Backup", ex_days: dict, per_ex_week: dict, pr: dict,
                   first_seen: dict, anchor: date) -> list[dict]:
    """最近続けている重量×回数の種目ごとに、推定1RM の伸び・停滞の状態と判断材料をまとめる。

    状態(直近4週は今週を含む):
    - up(伸び): 直近4週に自己ベストを更新した
    - new(記録開始): 直近4週に初めて記録し、自己ベストがまだ初回の記録のまま
      (初回を「更新」と数えると、始めたばかりの種目が必ず「伸び」に見えるため分ける)
    - down(下降): 直近4週の最高が自己ベストより5%以上低い
    - flat(横ばい): 上のどれでもない(自己ベストを4週以上更新していない)
    """
    this_monday = _monday(anchor)
    win_from = (this_monday - timedelta(weeks=PROGRESS_WINDOW_WEEKS - 1)).isoformat()
    prev_from = (this_monday - timedelta(weeks=2 * PROGRESS_WINDOW_WEEKS - 1)).isoformat()
    pick_from = (this_monday - timedelta(weeks=PROGRESS_PICK_WEEKS - 1)).isoformat()
    rows = []
    for ex_id, days in ex_days.items():
        e = bk.ex(ex_id)
        best = pr.get(ex_id)
        if not e or not best or best.get("e1rm") is None:
            continue
        if sum(1 for d in days if d >= pick_from) < PROGRESS_PICK_MIN_DAYS:
            continue
        recent = [v["e1rm"] for d, v in days.items() if d >= win_from]
        before = [v["e1rm"] for d, v in days.items() if prev_from <= d < win_from]
        recent_best = max(recent) if recent else None
        before_best = max(before) if before else None
        if best["date"] >= win_from:
            status = "new" if best["date"] == first_seen.get(ex_id) else "up"
        elif recent_best is not None and recent_best < best["e1rm"] * PROGRESS_DOWN_RATIO:
            status = "down"
        else:
            status = "flat"
        recent_days = sorted(d for d in days if d >= win_from)
        interval = None
        if len(recent_days) >= 2:
            span = (_pdate(recent_days[-1]) - _pdate(recent_days[0])).days
            interval = round(span / (len(recent_days) - 1), 1)
        last_day = max(days)
        last = days[last_day]
        cells = per_ex_week.get(ex_id, {})
        part = BODYPART_MAP.get(e["bodyPart"])
        rows.append({
            "id": ex_id,
            "name": e["name"],
            "part": part[0] if part else None,
            "status": status,
            "prE1rm": best["e1rm"], "prDate": best["date"],
            "prWeightKg": best["weightKg"], "prReps": best["reps"],
            "weeksSincePr": (this_monday - _monday(_pdate(best["date"]))).days // 7,
            "recentBest": recent_best,
            "delta": round(recent_best - before_best, 1)
                     if recent_best is not None and before_best is not None else None,
            "spark": [(cells.get(wi) or {}).get("e1rm")
                      for wi in range(N_WEEKS - PROGRESS_SPARK_WEEKS, N_WEEKS)],
            "recentDays": len(recent_days),
            "intervalDays": interval,
            "lastDate": last_day, "lastTopKg": last["top"], "lastTopReps": last["reps"],
        })
    rows.sort(key=lambda r: (PROGRESS_ORDER[r["status"]], -r["weeksSincePr"], r["name"]))
    return rows


class Backup:
    """BackupData JSON をそのまま保持し、索引を張るだけの薄いラッパー。"""

    def __init__(self, data: dict):
        self.raw = data
        self.exercises = {e["id"]: e for e in data.get("exercises", [])}
        self.workouts = {w["id"]: w for w in data.get("workouts", [])}
        self.sets = data.get("workoutSets", [])
        self.body = data.get("bodyMetrics", [])
        self.goals = data.get("goals", [])

    def ex(self, ex_id):
        return self.exercises.get(ex_id)

    def work_sets(self):
        """WORK セットに、所属ワークアウトの日付と種目情報を添えて返す。"""
        for s in self.sets:
            if s.get("setType") != "WORK":
                continue
            w = self.workouts.get(s["workoutId"])
            e = self.exercises.get(s["exerciseId"])
            if not w or not e:
                continue
            yield s, w, e


def _weeks_frame(anchor: date):
    """anchor の週(月曜)を最終週として、直近 N_WEEKS 週の月曜日リストを返す。"""
    last_monday = _monday(anchor)
    return [last_monday - timedelta(weeks=(N_WEEKS - 1 - i)) for i in range(N_WEEKS)]


def _week_index(weeks, d: date):
    """日付 d が weeks(月曜リスト)の何番目の週か。範囲外なら None。"""
    m = _monday(d)
    if m < weeks[0] or m > weeks[-1]:
        return None
    return (m - weeks[0]).days // 7


def build_data(data: dict, today: date | None = None) -> dict:
    bk = Backup(data)
    # 基準日: 記録の最新日と今日の遅いほう(未来の空週を作りすぎない)
    all_dates = [_pdate(w["date"]) for w in bk.workouts.values()] + \
                [_pdate(b["date"]) for b in bk.body]
    anchor = max([today or date.today()] + all_dates) if all_dates else (today or date.today())
    weeks = _weeks_frame(anchor)
    week_iso = [w.isoformat() for w in weeks]

    # --- 種目別: 週ごとのトップセット(最重量)と、その推定1RM ---
    # ex_id -> week_idx -> {"top": weightKg, "e1rm": ..., "reps": ...}
    per_ex_week: dict = defaultdict(dict)
    # 部位別 週セット数, カレンダー(日別総挙上量), PR
    vol_week = [defaultdict(int) for _ in weeks]          # week_idx -> part -> 本数
    calendar: dict = defaultdict(float)                    # 日付iso -> 総挙上量
    pr: dict = {}                                          # ex_id -> best set dict
    first_seen: dict = {}                                   # ex_id -> その種目を最初に記録した日(iso文字列)
    ex_days: dict = defaultdict(dict)                      # ex_id -> 日付iso -> {"top", "reps", "e1rm"}(重量×回数の種目のみ)

    for s, w, e in bk.work_sets():
        d = _pdate(w["date"])
        wi = _week_index(weeks, d)
        prev_first = first_seen.get(e["id"])
        if prev_first is None or w["date"] < prev_first:
            first_seen[e["id"]] = w["date"]
        wkg = s.get("weightKg")
        reps = s.get("reps")
        # カレンダー: 総挙上量。自重種目は追加重量ぶんのみ、時間/距離種目は寄与しない
        vol = set_volume_kg(s, e)
        if vol:
            calendar[w["date"]] += vol
        # 部位別セット数(6分類のみ)
        part = BODYPART_MAP.get(e["bodyPart"])
        if part and wi is not None:
            vol_week[wi][part[0]] += 1
        # 推定1RM とトップセット推移は「重量×回数」種目のみ。自重系(懸垂・腹筋など)は
        # weightKg が体重を含むうえ高回数になりやすく、推定1RMの式が非現実的な値を出すため。
        rt = e["recordType"]
        if rt == "WEIGHT_REPS" and wkg is not None and reps:
            e1 = epley_e1rm(wkg, reps)
            best = pr.get(e["id"])
            if best is None or e1 > (best.get("e1rm") or -1):
                pr[e["id"]] = {"weightKg": wkg, "reps": reps, "e1rm": e1,
                               "date": w["date"], "name": e["name"], "recordType": rt}
            # 週トップセット: その週で最重量のセット(同じ重量なら回数の多いほう)。
            # 推定1RM はトップセットからの換算ではなく、その週の全セットで最も高い値にする。
            # 自己ベスト(pr)と同じ物差しにそろえ、一覧表の自己ベストがグラフ上にも現れるようにするため。
            if wi is not None:
                _merge_best(per_ex_week[e["id"]], wi, wkg, reps, e1)
            _merge_best(ex_days[e["id"]], w["date"], wkg, reps, e1)
        elif rt == "BODYWEIGHT_REPS" and reps:
            # 自重系の PR は「追加重量 → 回数」の大きいほうを採用。推定1RM は出さない(None)。
            best = pr.get(e["id"])
            key = (wkg if wkg is not None else 0, reps)
            bkey = ((best.get("weightKg") or 0), (best.get("reps") or 0)) if best else None
            if best is None or key > bkey:
                pr[e["id"]] = {"weightKg": wkg, "reps": reps, "e1rm": None,
                               "date": w["date"], "name": e["name"], "recordType": rt}

    # --- BIG3 の種目ID解決(「成長の証拠」の推定1RM合計カード用) ---
    big3_ids = {}
    for key, _label, needles in BIG3:
        found = None
        for needle in needles:
            # 「バーベル版の種目が実在するか」ではなく「実際に記録したことがあるか」で絞る。
            # 種目マスタにはバーベル版が定義されているだけで一度も記録が無いことも多く、
            # そのまま優先すると(記録が無いので)合計が永久に値なしになってしまうため。
            # 一度もその優先種目を記録していなければ、次の needle(より広い一致)へフォールバックする。
            cands = [e for e in bk.exercises.values() if needle in e["name"]
                     and e["recordType"] == "WEIGHT_REPS" and e["id"] in per_ex_week]
            if cands:
                # 同じ needle に複数の種目名がマッチした場合(例: 「デッドリフト」は
                # バーベル・ダンベル・ルーマニアン・スモウの全種目名を含んでしまう)は、
                # 記録した週数が多い(継続的にやっている)ものを優先する。PR の推定1RM だけで
                # 選ぶと、たまたま高レップの1回だけ記録した種目に毎回選択先が飛んでしまい、
                # BIG3合計の元になる種目が記録のたびに入れ替わって履歴が積み上がらなくなるため。
                cands.sort(key=lambda e: (len(per_ex_week.get(e["id"], {})),
                                           pr.get(e["id"], {}).get("e1rm", 0)), reverse=True)
                found = cands[0]["id"]
                break
        if found:
            big3_ids[key] = found

    # BIG3 推定1RM合計の週次系列。
    # 各種目ごとに「直前に記録した週の推定1RM」を持ち越して毎週合計を出す(3種目を同じ週に
    # やることは少なく、そろった週だけに絞ると点がほとんど打てないため)。
    # ただし持ち越した値を含む週は big3_fresh を False にし、フロント側で線を薄く/点を小さく
    # するなど見た目を変えられるようにする(「その週すべて実施した」実測点との違いを示すため)。
    # 3種目とも記録が無い期間(いずれかがまだ一度も記録されていない週)は None のまま。
    # BIG3 のどれかがそもそも記録に無い(種目IDが解決できない)場合も、全週 None になる。
    big3_complete = len(big3_ids) == len(BIG3)
    big3_series = []
    big3_fresh = []
    if big3_complete:
        carried = {}
        for ex_id in big3_ids.values():
            cells = per_ex_week.get(ex_id, {})
            last = None
            row = []
            for wi in range(N_WEEKS):
                entry = cells.get(wi)
                fresh = entry is not None
                if fresh:
                    last = entry["e1rm"]
                row.append((last, fresh))
            carried[ex_id] = row
        for wi in range(N_WEEKS):
            row = [carried[ex_id][wi] for ex_id in big3_ids.values()]
            if all(v is not None for v, _fresh in row):
                big3_series.append(round(sum(v for v, _fresh in row), 1))
                big3_fresh.append(all(fresh for _v, fresh in row))
            else:
                big3_series.append(None)
                big3_fresh.append(False)
    else:
        big3_series = [None] * N_WEEKS
        big3_fresh = [False] * N_WEEKS

    # --- 月次ボリューム(積み上げバー用): 週セット数を月にまとめる ---
    monthly = {}
    for wi, wk in enumerate(weeks):
        ym = (wk.year, wk.month)
        acc = monthly.setdefault(ym, {p: 0 for p in PART_ORDER})
        for p in PART_ORDER:
            acc[p] += vol_week[wi].get(p, 0)
    monthly_labels = [f"{y}-{m:02d}" for (y, m) in sorted(monthly)]
    monthly_rows = [monthly[k] for k in sorted(monthly)]

    # --- 体組成: 週平均 ---
    body_by_week_w = [[] for _ in weeks]
    body_by_week_f = [[] for _ in weeks]
    for b in bk.body:
        wi = _week_index(weeks, _pdate(b["date"]))
        if wi is None:
            continue
        if b.get("weightKg") is not None:
            body_by_week_w[wi].append(b["weightKg"])
        if b.get("bodyFatPct") is not None:
            body_by_week_f[wi].append(b["bodyFatPct"])
    def _avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None
    weight_series = [_avg(xs) for xs in body_by_week_w]
    fat_series = [_avg(xs) for xs in body_by_week_f]

    # --- 目標 ---
    g = bk.goals[0] if bk.goals else {}
    goal = {
        "direction": g.get("direction"),
        "targetWeightKg": g.get("targetWeightKg"),
        "targetBodyFatPct": g.get("targetBodyFatPct"),
        "deadline": g.get("deadline"),
        "weeklyTargetCount": g.get("weeklyTargetCount", 3),
    }

    # --- 直近ワークアウト(最新5件) ---
    recent = _recent_workouts(bk, limit=5)

    # --- ヒーロー: 総挙上量(今週・今月・今年 と、その前の期間) ---
    # calendar(日別の総挙上量)から期間で切り出すだけ。
    def _lifted(d_from: date, d_to: date) -> float:
        """d_from 〜 d_to(両端を含む)の総挙上量 kg。"""
        return round(sum(v for k, v in calendar.items() if d_from <= _pdate(k) <= d_to), 1)

    def _clamp_day(y: int, m: int, d: int) -> date:
        """存在しない日付(2月30日など)は、その月の末日に丸めて返す。"""
        while True:
            try:
                return date(y, m, d)
            except ValueError:
                d -= 1

    # 比較相手は「先月・先週・昨年の**同じ時点まで**」にそろえる。
    # 進行中の今月を、終わったばかりの先月まるごとと比べると必ず負けて見えるため。
    # ただし「先月まるごとではいくつだったか」も知りたいので、Full 付きの値も一緒に返し、
    # 画面ではマウスを乗せたときの補足として出す(どちらの見方も取れるようにするため)。
    prev_m_year, prev_m_month = (anchor.year, anchor.month - 1) if anchor.month > 1 else (anchor.year - 1, 12)
    month_prev_from = date(prev_m_year, prev_m_month, 1)
    month_prev_to = _clamp_day(prev_m_year, prev_m_month, anchor.day)
    month_prev_end = anchor.replace(day=1) - timedelta(days=1)   # 先月の末日
    week_prev_from = _monday(anchor) - timedelta(days=7)
    week_prev_to = anchor - timedelta(days=7)

    lifted = {
        "week": _lifted(_monday(anchor), anchor),
        "month": _lifted(anchor.replace(day=1), anchor),
        "monthLabel": f"{anchor.month}月",
        "year": _lifted(date(anchor.year, 1, 1), anchor),
        "yearLabel": f"{anchor.year}年",
        "asOf": anchor.isoformat(),
        "weekPrev": _lifted(week_prev_from, week_prev_to),
        "weekPrevTo": week_prev_to.isoformat(),
        "weekPrevFull": _lifted(week_prev_from, week_prev_from + timedelta(days=6)),
        "monthPrev": _lifted(month_prev_from, month_prev_to),
        "monthPrevLabel": f"{prev_m_month}月",
        "monthPrevTo": month_prev_to.isoformat(),
        "monthPrevFull": _lifted(month_prev_from, month_prev_end),
        "yearPrev": _lifted(date(anchor.year - 1, 1, 1), _clamp_day(anchor.year - 1, anchor.month, anchor.day)),
        "yearPrevLabel": f"{anchor.year - 1}年",
        "yearPrevTo": _clamp_day(anchor.year - 1, anchor.month, anchor.day).isoformat(),
        "yearPrevFull": _lifted(date(anchor.year - 1, 1, 1), date(anchor.year - 1, 12, 31)),
    }

    ai = _ai_plan(bk)

    return {
        "weeks": week_iso,
        "big3Ids": big3_ids,
        "big3Series": big3_series,
        "big3Fresh": big3_fresh,
        "perExercise": {str(ex_id): [per_ex_week.get(ex_id, {}).get(wi) for wi in range(N_WEEKS)]
                        for ex_id in per_ex_week},
        "exerciseNames": {str(e["id"]): e["name"] for e in bk.exercises.values()},
        "volumeWeek": [{p: wk.get(p, 0) for p in PART_ORDER} for wk in vol_week],
        "monthlyLabels": monthly_labels,
        "monthlyRows": monthly_rows,
        "partOrder": PART_ORDER,
        "partLabel": {v[0]: v[1] for v in BODYPART_MAP.values()},
        "calendar": {k: round(v) for k, v in calendar.items()},
        "weightSeries": weight_series,
        "fatSeries": fat_series,
        "goal": goal,
        "pr": sorted(_with_is_first(pr, first_seen), key=lambda x: (x["e1rm"] is not None, x["e1rm"] or 0),
                     reverse=True),
        "recent": recent,
        "progress": _progress_rows(bk, ex_days, per_ex_week, pr, first_seen, anchor),
        "progressWindowWeeks": PROGRESS_WINDOW_WEEKS,
        "anchor": anchor.isoformat(),
        "lifted": lifted,
        "aiPlan": ai,
    }


def _fmt_num(x):
    """40.0 → '40'、42.5 → '42.5' のように末尾の .0 を落とす。"""
    if x is None:
        return None
    return f"{x:g}"


def _summarize_planned(work_sets):
    """計画セット(WORK)を『3セット × 8–12回 @ 60.0–65.0kg』のような1行にまとめる。"""
    if not work_sets:
        return ""
    n = len(work_sets)
    rmins = [s["repsMin"] for s in work_sets if s.get("repsMin") is not None]
    rmaxs = [s["repsMax"] for s in work_sets if s.get("repsMax") is not None]
    weights = [s["weight"] for s in work_sets if s.get("weight") is not None]
    durs = [s["durationSec"] for s in work_sets if s.get("durationSec") is not None]
    parts = [f"{n}セット"]
    if rmins or rmaxs:
        lo = min(rmins) if rmins else min(rmaxs)
        hi = max(rmaxs) if rmaxs else max(rmins)
        parts.append(f"× {lo}–{hi}回" if lo != hi else f"× {lo}回")
    elif durs:
        parts.append(f"× {min(durs)//60 if min(durs)>=60 else min(durs)}"
                     + ("分" if min(durs) >= 60 else "秒"))
    if weights:
        lo, hi = min(weights), max(weights)
        unit = next((s.get("unit") for s in work_sets if s.get("unit")), "KG")
        u = "kg" if unit == "KG" else "lb"
        parts.append(f"@ {_fmt_num(lo)}–{_fmt_num(hi)}{u}" if lo != hi else f"@ {_fmt_num(lo)}{u}")
    return " ".join(parts)


def _ai_plan(bk: Backup):
    """最新世代の AI 週間プランを、日ごと・種目ごとに組み立てる。無ければ None。"""
    sessions = {s["id"]: s for s in bk.raw.get("aiSessions", [])}
    pws = [p for p in bk.raw.get("plannedWorkouts", []) if p.get("status") != "SUPERSEDED"]
    if not pws:
        return None

    def sess_created(p):
        s = sessions.get(p["aiSessionId"])
        return s["createdAt"] if s else 0
    latest_sid = max(pws, key=sess_created)["aiSessionId"]
    gen = [p for p in pws if p["aiSessionId"] == latest_sid]
    gen.sort(key=lambda p: (p.get("orderInWeek") or 0, p.get("scheduledDate") or ""))

    items = defaultdict(list)
    for it in bk.raw.get("routineItems", []):
        items[it["routineId"]].append(it)
    psets = defaultdict(list)
    for ps in bk.raw.get("plannedSets", []):
        psets[ps["routineItemId"]].append(ps)

    days = []
    for p in gen:
        exs = []
        for it in sorted(items.get(p["routineId"], []), key=lambda x: x.get("orderIndex", 0)):
            e = bk.ex(it["exerciseId"])
            work = sorted([s for s in psets.get(it["id"], []) if s.get("setType") == "WORK"],
                          key=lambda s: s.get("setIndex", 0))
            exs.append({"name": e["name"] if e else "?",
                        "summary": _summarize_planned(work),
                        "restSec": it.get("restSec"), "targetRir": it.get("targetRir")})
        days.append({"label": p["label"], "date": p.get("scheduledDate"),
                     "status": p["status"], "note": p.get("note"), "exercises": exs})
    sess = sessions.get(latest_sid, {})
    return {"days": days, "adherence": sess.get("adherence"),
            "requestedFrequency": sess.get("requestedFrequency"),
            "createdAt": sess.get("createdAt")}


def _recent_workouts(bk: Backup, limit=5):
    out = []
    for w in sorted(bk.workouts.values(), key=lambda w: (w["date"], w.get("startedAt", 0)),
                    reverse=True)[:limit]:
        sets = [s for s in bk.sets if s["workoutId"] == w["id"] and s.get("setType") == "WORK"]
        by_ex = defaultdict(list)
        vol = 0.0
        for s in sets:
            by_ex[s["exerciseId"]].append(s)
            e = bk.ex(s["exerciseId"])
            if e:
                vol += set_volume_kg(s, e)
        exs = []
        for ex_id, ss in by_ex.items():
            e = bk.ex(ex_id)
            ss.sort(key=lambda s: s.get("setIndex", 0))
            exs.append({
                "name": e["name"] if e else str(ex_id),
                "sets": [{"weight": s.get("weight"), "unit": s.get("unit"),
                          "weightKg": s.get("weightKg"), "reps": s.get("reps"),
                          "rir": s.get("rir")} for s in ss],
            })
        dur = None
        if w.get("finishedAt") and w.get("startedAt"):
            dur = round((w["finishedAt"] - w["startedAt"]) / 60000)
        out.append({"date": w["date"], "note": w.get("note"),
                    "volume": round(vol), "durationMin": dur, "exercises": exs})
    return out


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "sample-data/latest.json"
    with open(path, encoding="utf-8") as f:
        d = build_data(json.load(f))
    print(f"対象週: {d['weeks'][0]} 〜 {d['weeks'][-1]} ({len(d['weeks'])}週)")
    lf = d['lifted']
    print(f"総挙上量: 今週 {lf['week']:,.0f}kg / 今月 {lf['month']:,.0f}kg / 今年 {lf['year']:,.0f}kg")
    print(f"BIG3 種目ID: {d['big3Ids']}")
    b3 = [v for v in d['big3Series'] if v is not None]
    print(f"BIG3 推定1RM合計: 最新 {b3[-1] if b3 else None} kg(12ヶ月前 {d['big3Series'][0]})")
    print(f"体重(週平均・直近で非None): "
          f"{next((v for v in reversed(d['weightSeries']) if v is not None), None)} kg / "
          f"目標 {d['goal']['targetWeightKg']}(方向 {d['goal']['direction']})")
    print(f"PR 件数: {len(d['pr'])}  上位3:")
    for p in d['pr'][:3]:
        print(f"  {p['name']}: {p['weightKg']}kg×{p['reps']} → 推定1RM {p['e1rm']}kg ({p['date']})")
    print(f"直近ワークアウト {len(d['recent'])} 件:")
    for r in d['recent']:
        print(f"  {r['date']}  {len(r['exercises'])}種目  総挙上量 {r['volume']}kg  {r['durationMin']}分")
