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
    ("dead",  "デッドリフト", ["デッドリフト"]),
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

    for s, w, e in bk.work_sets():
        d = _pdate(w["date"])
        wi = _week_index(weeks, d)
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
            # 週トップセット: その週で最重量のセット
            cur = per_ex_week[e["id"]].get(wi)
            if wi is not None and (cur is None or wkg > cur["top"]):
                per_ex_week[e["id"]][wi] = {"top": wkg, "reps": reps, "e1rm": e1}
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
            cands = [e for e in bk.exercises.values() if needle in e["name"]
                     and e["recordType"] == "WEIGHT_REPS"]
            if cands:
                # 実際に記録があるものを優先、その中で PR の高い順
                cands.sort(key=lambda e: pr.get(e["id"], {}).get("e1rm", 0), reverse=True)
                found = cands[0]["id"]
                break
        if found:
            big3_ids[key] = found

    # BIG3 推定1RM合計の週次系列。
    # 値を出すのは「その週に3種目すべてを実施した週」だけで、1種目でも欠けた週は None(点を打たない)。
    # 以前は種目ごとに直前の値を持ち越して足していたが、それだとベンチだけやった週の合計が
    # スクワット・デッドリフトの過去の値で埋められ、「その週の3種目の力の合計」ではなくなっていた。
    # また BIG3 のどれかがそもそも記録に無い(種目IDが解決できない)場合は、全週 None になる。
    big3_complete = len(big3_ids) == len(BIG3)
    big3_series = []
    for wi in range(N_WEEKS):
        cells = [per_ex_week.get(ex_id, {}).get(wi) for ex_id in big3_ids.values()]
        if big3_complete and all(c and c.get("e1rm") is not None for c in cells):
            big3_series.append(round(sum(c["e1rm"] for c in cells), 1))
        else:
            big3_series.append(None)

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
        "perExercise": {str(ex_id): [per_ex_week.get(ex_id, {}).get(wi) for wi in range(N_WEEKS)]
                        for ex_id in per_ex_week},
        "exerciseNames": {str(e["id"]): e["name"] for e in bk.exercises.values()},
        "volumeWeek": [{p: wk.get(p, 0) for p in PART_ORDER} for wk in vol_week],
        "monthlyLabels": monthly_labels,
        "monthlyRows": monthly_rows,
        "partOrder": PART_ORDER,
        "partLabel": {v[0]: v[1] for v in BODYPART_MAP.values()},
        "calendar": dict(calendar),
        "weightSeries": weight_series,
        "fatSeries": fat_series,
        "goal": goal,
        "pr": sorted(pr.values(), key=lambda x: (x["e1rm"] is not None, x["e1rm"] or 0),
                     reverse=True),
        "recent": recent,
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
