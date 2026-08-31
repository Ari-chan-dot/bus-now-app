"""
フェーズ3+5: バックエンドサーバー(FastAPI)

役割:
  1. 起動時に静的GTFS(フェーズ1の資産)を読み込む
  2. バックグラウンドで数十秒おきにTripUpdates(フェーズ2の資産)を取得し、
     trip_idごとの遅延(秒)をメモリ上にキャッシュし続ける
  3. GET /buses で、有効な系統ぶんの「あと何分(遅延反映込み)」をJSONで返す
  4. フェーズ5: index.html・manifest.json・アイコンも配信し、フロントエンドと
     バックエンドを1つのサーバーにまとめる(公開デプロイをシンプルにするため)

実行方法(ローカル):
  pip3 install fastapi uvicorn requests gtfs-realtime-bindings pandas
  export ODPT_TOKEN="実際のアクセストークン"
  python3 -m uvicorn server:app --reload
  → ブラウザで http://127.0.0.1:8000/ を開く

注意:
  このファイルは phase1_next_buses.py・index.html・manifest.json・icons/ と
  同じフォルダに置いてください。
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from typing import Optional

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.transit import gtfs_realtime_pb2

from phase1_next_buses import ROUTE_DEFS, get_next_buses

# Renderのサーバーはアメリカのタイムゾーン(UTC)で動いているため、
# 「今何時か」は常に日本時間(JST)を明示して取得する
JST = ZoneInfo("Asia/Tokyo")

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
# トークンは環境変数 ODPT_TOKEN から読む(公開先にコードごと置いても安全なように)。
# ローカルで試すときは、実行前に export ODPT_TOKEN="..." を忘れずに。
TOKEN = os.environ.get("ODPT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "環境変数 ODPT_TOKEN が設定されていません。\n"
        "ローカルで実行する場合は、先にターミナルで次を実行してからサーバーを起動してください:\n"
        '  export ODPT_TOKEN="実際のアクセストークン"'
    )
TRIP_UPDATE_URL = (
    "https://api.odpt.org/api/v4/gtfs/realtime/"
    f"odpt_KeioBus_AllLines_trip_update?acl:consumerKey={TOKEN}"
)
FETCH_INTERVAL_SECONDS = 30  # ODPTへの負荷も考え、まずは30秒おきから

# 遅延キャッシュ: { trip_id: delay_seconds }
delay_cache: dict = {}
cache_updated_at: Optional[datetime] = None


# ------------------------------------------------------------------
# フェーズ2の資産: 取得して trip_id -> delay(秒) の辞書にする
# ------------------------------------------------------------------
def fetch_delay_by_trip_id():
    response = requests.get(TRIP_UPDATE_URL, timeout=10)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    result = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        for stu in tu.stop_time_update:
            if stu.HasField("arrival") and stu.arrival.HasField("delay"):
                result[tu.trip.trip_id] = stu.arrival.delay
    return result


async def refresh_loop():
    """バックグラウンドで動き続け、定期的にキャッシュを更新する。"""
    global delay_cache, cache_updated_at
    while True:
        try:
            delay_cache = fetch_delay_by_trip_id()
            cache_updated_at = datetime.now(JST)
            print(f"[{cache_updated_at:%H:%M:%S}] 遅延キャッシュ更新: {len(delay_cache)}件")
        except Exception as e:
            # 取得に失敗しても、直前のキャッシュを使い続ける(アプリを止めない)
            print(f"[警告] 遅延取得に失敗、直前のキャッシュを維持します: {e}")
        await asyncio.sleep(FETCH_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(refresh_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

# 別オリジンから呼ばれる場合(将来的な拡張)に備え、CORSも引き続き許可しておく
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# フェーズ5: フロントエンド一式もこのサーバーから配信する
app.mount("/icons", StaticFiles(directory="icons"), name="icons")


@app.get("/")
def serve_index():
    return FileResponse("index.html")


@app.get("/manifest.json")
def serve_manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")


# ------------------------------------------------------------------
# フェーズ1(静的時刻表)とフェーズ2(遅延キャッシュ)を trip_id で統合する
# ------------------------------------------------------------------
def apply_delay(bus, route_def):
    if not route_def["has_realtime"]:
        bus["status"] = "時刻表どおり(リアルタイム非対応)"
        return bus

    delay = delay_cache.get(bus["trip_id"])
    if delay is None:
        bus["status"] = "リアルタイム情報なし(定刻扱い)"
        return bus

    delay_minutes = round(delay / 60)
    bus["minutes_from_now"] += delay_minutes
    if delay_minutes > 0:
        bus["status"] = f"{delay_minutes}分遅れ"
    elif delay_minutes < 0:
        bus["status"] = f"{abs(delay_minutes)}分早着"
    else:
        bus["status"] = "定刻どおり"
    return bus


# リアルタイム対応の系統では、予定時刻がこれだけ過去でも「遅延でまだ来ていない
# かもしれない候補」として拾っておく(実際に来たかどうかは遅延反映後に判定する)
DELAY_LOOKBACK_MINUTES = 60
CANDIDATE_COUNT = 10


@app.get("/buses")
def get_buses():
    now = datetime.now(JST)
    routes_output = []
    for rd in ROUTE_DEFS:
        if not rd["enabled"]:
            continue

        lookback = DELAY_LOOKBACK_MINUTES if rd["has_realtime"] else 0
        candidates = get_next_buses(rd, now, n=CANDIDATE_COUNT, lookback_minutes=lookback)
        candidates = [apply_delay(b, rd) for b in candidates]

        # 遅延を反映した「実際の到着予定」がまだ来ていない(0分以上先)ものだけを残し、
        # その実際の時刻順に並べ直してから、改めて3本選ぶ
        upcoming = [b for b in candidates if b["minutes_from_now"] >= 0]
        upcoming.sort(key=lambda b: b["minutes_from_now"])
        buses = upcoming[:3]

        # 表示用: 遅延を反映した「到着予想時刻」を時計表示(HH:MM)で計算しておく
        # (元の予定時刻 + 遅延分。now基準の相対分数から逆算するので日またぎも自然に処理できる)
        for b in buses:
            estimated_dt = now + timedelta(minutes=b["minutes_from_now"])
            b["estimated_time"] = estimated_dt.strftime("%H:%M")

        routes_output.append({
            "company": rd["company"],
            "direction": rd["direction"],
            "buses": buses,
        })

    # enabled=False の系統しか無い会社は「準備中」としてフロントに伝える
    # (フェーズ5の考え方どおり、小田急バスが再開したら自動的にここから消える)
    enabled_companies = {r["company"] for r in routes_output}
    all_companies = {rd["company"] for rd in ROUTE_DEFS}
    paused_companies = sorted(all_companies - enabled_companies)

    return {
        "as_of": now.isoformat(timespec="seconds"),
        "paused_companies": paused_companies,
        "realtime_cache_updated_at": (
            cache_updated_at.isoformat(timespec="seconds") if cache_updated_at else None
        ),
        "routes": routes_output,
    }
