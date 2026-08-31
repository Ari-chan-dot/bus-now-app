"""
フェーズ1：GTFS静的データから「現在時刻から見て先3本分」の予定到着時刻を算出する

考え方:
  1. 対象の停留所・系統は ROUTE_DEFS にデータとして持たせる(ハードコードしない)
     → フェーズ5で路線を追加するときも、ここに1行足すだけでよい設計
  2. calendar.txt(曜日パターン) と calendar_dates.txt(日付ごとの例外) の
     両方に対応した service_id 判定を共通関数として用意する
  3. 停留所(stop_id)で stop_times.txt を絞り込み、必要なら route_id でも絞り込む
  4. 現在時刻(分に変換)以降のものだけ残し、時刻順に並べて先頭3件を取り出す

注意(既知の制限):
  - 深夜便(24:00以降表記, 例 25:10:00 = 翌1:10)は「当日のサービスの続き」として
    そのまま計算できるが、逆に「日付が変わった直後(例:00:20)に、前日サービスの
    24:20扱いの深夜便」を拾う処理は今回のMVPでは省略している。フェーズ2以降で
    必要になったら拡張する。
"""
import os
import pandas as pd
from datetime import datetime

WEEKDAY_COLS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

# ------------------------------------------------------------------
# フェーズ0で確定した対象5系統(ここを増やせば路線追加できる = フェーズ5の下準備)
# ------------------------------------------------------------------
ROUTE_DEFS = [
    {
        "company": "京王バス",
        "direction": "佐須→調布駅北口",
        "data_dir": "gtfs/keio",
        "stop_id": "0450_02",
        "route_filter": None,
        "has_realtime": True,   # 京王バスはGTFS-RT提供あり(フェーズ2で使う)
    },
    {
        "company": "京王バス",
        "direction": "調布駅北口→佐須",
        "data_dir": "gtfs/keio",
        "stop_id": "0450_01",
        "route_filter": None,
        "has_realtime": True,
    },
    {
        "company": "京王バス",
        "direction": "佐須→三鷹",
        "data_dir": "gtfs/keio",
        "stop_id": "0450_01",
        "route_filter": "1540",  # 鷹66のみ抽出
        "has_realtime": True,
    },
    {
        "company": "小田急バス",
        "direction": "佐須→調布駅北口",
        "data_dir": "gtfs/odakyu",
        "stop_id": "10719_2",
        "route_filter": None,
        "has_realtime": False,  # 小田急バスはGTFS-RT非提供(2026年8月時点)
        "enabled": False,       # 2026-08時点: GTFSの有効期限切れ(〜7/31)のため停止中
        "disabled_reason": "GTFSデータが古い(有効期限〜2026/07/31)。ODPTから最新版を取得し、"
                            "gtfs/odakyu/ の該当ファイルを差し替えたうえで、"
                            "このenabledをTrueに変更すれば再開できる。",
    },
    {
        "company": "小田急バス",
        "direction": "調布駅北口→佐須",
        "data_dir": "gtfs/odakyu",
        "stop_id": "10719_1",
        "route_filter": None,
        "has_realtime": False,
        "enabled": False,
        "disabled_reason": "GTFSデータが古い(有効期限〜2026/07/31)。ODPTから最新版を取得し、"
                            "gtfs/odakyu/ の該当ファイルを差し替えたうえで、"
                            "このenabledをTrueに変更すれば再開できる。",
    },
]

# 京王バス3系統には enabled キーを明示していないので、ここで既定値Trueを補っておく
for _rd in ROUTE_DEFS:
    _rd.setdefault("enabled", True)
    _rd.setdefault("disabled_reason", None)


def load_gtfs(data_dir):
    """指定フォルダからGTFSファイルを読み込む。calendar系はどちらか片方だけでもよい。"""
    def read(name, required=True):
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            if required:
                raise FileNotFoundError(f"{path} が見つかりません")
            return None
        return pd.read_csv(path, dtype=str)

    return {
        "stop_times": read("stop_times.txt"),
        "trips": read("trips.txt"),
        "routes": read("routes.txt"),
        "calendar": read("calendar.txt", required=False),
        "calendar_dates": read("calendar_dates.txt", required=False),
    }


def get_valid_service_ids(date_obj, calendar, calendar_dates):
    """指定日に有効なservice_idの集合を返す(GTFS標準ロジック)。"""
    date_str = date_obj.strftime("%Y%m%d")
    weekday_col = WEEKDAY_COLS[date_obj.weekday()]
    valid = set()

    if calendar is not None:
        mask = (
            (calendar["start_date"] <= date_str)
            & (calendar["end_date"] >= date_str)
            & (calendar[weekday_col] == "1")
        )
        valid |= set(calendar[mask]["service_id"])

    if calendar_dates is not None:
        today_ex = calendar_dates[calendar_dates["date"] == date_str]
        added = set(today_ex[today_ex["exception_type"] == "1"]["service_id"])
        removed = set(today_ex[today_ex["exception_type"] == "2"]["service_id"])
        valid = (valid | added) - removed

    return valid


def time_to_minutes(t):
    """'25:10:00' のような24時超え表記も含めて、分単位の数値に変換する。"""
    h, m, _s = t.split(":")
    return int(h) * 60 + int(m)


def get_next_buses(route_def, as_of, n=3, lookback_minutes=0):
    """
    as_ofから見た次の便を最大n件返す。

    lookback_minutes: 予定時刻がこの分数だけ過去のものも候補に含める。
    リアルタイムの遅延で「予定時刻は過ぎたがまだ来ていない」便を、
    server.py側で遅延を反映してから拾い直せるようにするための余白。
    """
    g = load_gtfs(route_def["data_dir"])
    valid_services = get_valid_service_ids(as_of.date(), g["calendar"], g["calendar_dates"])

    trips = g["trips"][["trip_id", "route_id", "service_id"]]
    trips_today = trips[trips["service_id"].isin(valid_services)]
    if route_def["route_filter"]:
        trips_today = trips_today[trips_today["route_id"] == route_def["route_filter"]]

    st = g["stop_times"][g["stop_times"]["stop_id"] == route_def["stop_id"]][["trip_id", "arrival_time"]]

    routes_cols = [c for c in ["route_id", "route_short_name", "route_long_name"] if c in g["routes"].columns]
    merged = st.merge(trips_today, on="trip_id").merge(g["routes"][routes_cols], on="route_id", how="left")

    merged["minutes"] = merged["arrival_time"].apply(time_to_minutes)
    now_minutes = as_of.hour * 60 + as_of.minute

    upcoming = merged[merged["minutes"] >= now_minutes - lookback_minutes].sort_values("minutes").head(n)

    results = []
    for _, row in upcoming.iterrows():
        name = row.get("route_short_name") or row.get("route_long_name") or ""
        results.append({
            "trip_id": row["trip_id"],
            "arrival_time": row["arrival_time"][:5],
            "minutes_from_now": int(row["minutes"] - now_minutes),
            "route_name": str(name).strip(),
        })
    return results


def print_all(as_of):
    print(f"=== {as_of.strftime('%Y-%m-%d %H:%M')} 時点 ===\n")
    paused = []
    for rd in ROUTE_DEFS:
        if not rd["enabled"]:
            paused.append(rd)
            continue
        print(f"【{rd['company']}】{rd['direction']}")
        try:
            buses = get_next_buses(rd, as_of)
            if not buses:
                print("  本日はこれ以降の便がありません")
            for b in buses:
                print(f"  {b['arrival_time']}発 (あと{b['minutes_from_now']}分) [{b['route_name']}]")
        except FileNotFoundError as e:
            print(f"  ⚠ データ未取得: {e}")
        print()

    if paused:
        print("--- 現在停止中(データ準備中)の路線 ---")
        for rd in paused:
            print(f"【{rd['company']}】{rd['direction']} : {rd['disabled_reason']}")


if __name__ == "__main__":
    # デモ用: 京王バスのデータが対象としている 2026-08-26(水) 8:00 時点で実行
    print_all(datetime(2026, 8, 26, 8, 0))
