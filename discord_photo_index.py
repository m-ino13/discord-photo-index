#!/usr/bin/env python3
"""写真付きメッセージ(画像N枚以上)へのリンク一覧を、指定チャンネルに投稿するワンショットスクリプト。

設定ファイル(このスクリプトと同じフォルダに置く):
    .env          DISCORD_TOKEN を書く(Git管理しない)
    .env.custom   サーバーID・出力先チャンネルID・枚数・時間差などの設定(Git管理する)
環境変数は使わず、上の2ファイルから直接読み込む。

使い方:
    python discord_photo_index.py --dry-run   # 投稿せず標準出力に表示
    python discord_photo_index.py             # 実際に投稿

ゲートウェイには接続せず、REST APIだけを使う(既存Botと同時に動かしても干渉しない)。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from dotenv import dotenv_values
from discord.http import Route
from discord.utils import snowflake_time

BASE_DIR = Path(__file__).resolve().parent
SECRETS_FILE = BASE_DIR / ".env"           # トークン(Git管理しない)
SETTINGS_FILE = BASE_DIR / ".env.custom"   # 設定(Git管理する)

# .env.custom で省略したときの既定値
DEFAULT_MIN_IMAGES = 5          # 画像N枚以上のメッセージを対象にする
DEFAULT_GAP_HOURS = 3.0         # 前回採用した投稿からこの時間以内(または同日)なら除外
DEFAULT_TIMEZONE = "Asia/Tokyo"  # 「同日」判定・表示日付のタイムゾーン

PAGE_LIMIT = 25              # 検索APIの1回あたり最大件数
MAX_OFFSET = 9975            # 検索APIのoffset上限
SEARCH_INTERVAL_SEC = 1.0    # 検索リクエスト間の待ち時間
MAX_INDEX_RETRIES = 10       # 202(インデックス未準備)時のリトライ上限
MSG_CHAR_LIMIT = 1900        # 1投稿あたりの文字数(上限2000に余裕を持たせる)
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "avif", "bmp"}


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int              # 検索対象サーバーのID
    output_channel_id: int     # 一覧の投稿先チャンネルID
    min_images: int
    gap_hours: float
    timezone: ZoneInfo


def _read_setting(values: dict, key: str, cast, default=None):
    """.env.custom から1項目読む。defaultがNoneの項目は必須。"""
    raw = (values.get(key) or "").strip()
    if not raw:
        if default is None:
            sys.exit(f"{SETTINGS_FILE.name} に {key} を設定してください。")
        return default
    try:
        return cast(raw)
    except (ValueError, KeyError):  # ZoneInfoNotFoundError は KeyError の一種
        sys.exit(f"{SETTINGS_FILE.name} の {key} の値が正しくありません: {raw!r}")


def load_settings() -> Settings:
    """.env と .env.custom を直接読み込む(環境変数は使わない)。"""
    for path in (SECRETS_FILE, SETTINGS_FILE):
        if not path.exists():
            sys.exit(f"設定ファイルが見つかりません: {path}")

    token = (dotenv_values(SECRETS_FILE).get("DISCORD_TOKEN") or "").strip()
    if not token:
        sys.exit(f"{SECRETS_FILE.name} に DISCORD_TOKEN を設定してください。")

    custom = dotenv_values(SETTINGS_FILE)
    return Settings(
        token=token,
        guild_id=_read_setting(custom, "GUILD_ID", int),
        output_channel_id=_read_setting(custom, "OUTPUT_CHANNEL_ID", int),
        min_images=_read_setting(custom, "MIN_IMAGES", int, DEFAULT_MIN_IMAGES),
        gap_hours=_read_setting(custom, "GAP_HOURS", float, DEFAULT_GAP_HOURS),
        timezone=_read_setting(custom, "TIMEZONE", ZoneInfo, ZoneInfo(DEFAULT_TIMEZONE)),
    )


@dataclass(frozen=True)
class Hit:
    id: int
    channel_id: int
    created_at: datetime  # UTC (aware)


# --------------------------- 検索 ---------------------------
def count_images(message: dict) -> int:
    """添付ファイルのうち画像のものの数を数える(動画・URL埋め込みは数えない)。"""
    n = 0
    for att in message.get("attachments", []):
        content_type = att.get("content_type") or ""
        if content_type:
            is_image = content_type.startswith("image/")
        else:  # content_type が無い古い添付は拡張子で判定
            ext = os.path.splitext(att.get("filename", ""))[1].lower().lstrip(".")
            is_image = ext in IMAGE_EXTENSIONS
        n += is_image
    return n


async def search_page(client: discord.Client, guild_id: int, params: list[tuple]) -> dict:
    """検索APIを1回叩く。インデックス未準備(202)ならretry_after分待って再試行。"""
    route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=guild_id)
    for _ in range(MAX_INDEX_RETRIES):
        data = await client.http.request(route, params=params)
        if "messages" in data:
            return data
        wait = max(float(data.get("retry_after") or 0), 1.0)  # 0の場合も短く待つ
        print(f"\n検索インデックス準備中。{wait:.1f}秒待機します...")
        await asyncio.sleep(wait)
    raise RuntimeError("検索インデックスの準備が完了しませんでした。しばらくして再実行してください。")


def fmt_duration(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def collect_hits(client: discord.Client, guild_id: int, min_images: int) -> list[Hit]:
    """has=image で検索し、画像がmin_images枚以上のメッセージだけを集める。"""
    found: dict[int, Hit] = {}
    oldest_seen: int | None = None
    max_id: int | None = None  # offset上限を超えたとき、ここから先を新しい窓として検索する
    offset = 0
    deep_indexing = False
    start = time.monotonic()
    base = 0  # offset上限で窓をずらす前に処理し終えた件数(進捗表示用)

    while True:
        params: list[tuple] = [
            ("has", "image"),
            ("limit", PAGE_LIMIT),
            ("offset", offset),
            ("sort_by", "timestamp"),
            ("sort_order", "desc"),
        ]
        if max_id is not None:
            params.append(("max_id", max_id))

        data = await search_page(client, guild_id, params)
        deep_indexing = deep_indexing or bool(data.get("doing_deep_historical_index"))
        groups = data.get("messages", [])
        total = data.get("total_results", 0)

        # messages は「メッセージ配列の配列」。平坦化して処理する
        for group in groups:
            for m in group:
                mid = int(m["id"])
                oldest_seen = mid if oldest_seen is None else min(oldest_seen, mid)
                if count_images(m) >= min_images:
                    found[mid] = Hit(mid, int(m["channel_id"]), snowflake_time(mid))

        # 返却件数はlimitより少ないことがあるので、件数ではなくoffsetで進める
        offset += PAGE_LIMIT

        # 進捗表示(ターミナルの同じ行を上書き)
        done = base + min(offset, total)
        grand_total = base + total
        elapsed = time.monotonic() - start
        eta = elapsed / done * (grand_total - done) if done else 0
        print(
            f"\r検索中: {done:,}/{grand_total:,}件 ({done / max(grand_total, 1):.0%}) "
            f"| 該当 {len(found):,}件 | 経過 {fmt_duration(elapsed)} | 残り約 {fmt_duration(eta)}    ",
            end="",
            flush=True,
        )

        if not groups or offset >= total:
            break
        if offset > MAX_OFFSET:
            base += offset
            max_id, offset = oldest_seen, 0
        await asyncio.sleep(SEARCH_INTERVAL_SEC)
    print()  # 進捗行の改行

    if deep_indexing:
        print("注意: サーバーが過去ログの再インデックス中です。古い投稿に取りこぼしがある可能性があるため、"
              "時間をおいて再実行し、件数を比較してください。")
    return list(found.values())


# --------------------------- 連投の除外 ---------------------------
def is_same_burst(prev: datetime, cur: datetime, gap: timedelta, tz: tzinfo) -> bool:
    """同じ日(tz基準)、または時間差がgap以下なら連投とみなす。"""
    same_day = prev.astimezone(tz).date() == cur.astimezone(tz).date()
    return same_day or (cur - prev) <= gap


def drop_bursts(hits: list[Hit], gap: timedelta, tz: tzinfo) -> list[Hit]:
    """時系列に舐めて、直前に「採用した」投稿との比較で連投を除外する。"""
    kept: list[Hit] = []
    last: Hit | None = None
    for h in sorted(hits, key=lambda h: (h.created_at, h.id)):
        if last is not None and is_same_burst(last.created_at, h.created_at, gap, tz):
            continue
        kept.append(h)
        last = h
    return kept


# --------------------------- 出力 ---------------------------
async def resolve_channel_names(client: discord.Client, ids: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    for cid in ids:
        try:
            ch = await client.fetch_channel(cid)
            names[cid] = getattr(ch, "name", None) or str(cid)
        except discord.HTTPException:
            names[cid] = str(cid)
    return names


def chunk_lines(lines: list[str], limit: int) -> list[str]:
    chunks, buf = [], ""
    for line in lines:
        if buf and len(buf) + 1 + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


async def run(
    client: discord.Client, settings: Settings, min_images: int, gap_hours: float, dry_run: bool
) -> None:
    gap = timedelta(hours=gap_hours)
    tz = settings.timezone

    # 1. 検索して画像N枚以上のメッセージを収集
    hits = await collect_hits(client, settings.guild_id, min_images)
    print(f"画像{min_images}枚以上のメッセージ: {len(hits)}件")

    # 2-3. チャンネルごとに分け、時系列に連投を除外
    by_channel: dict[int, list[Hit]] = defaultdict(list)
    for h in hits:
        by_channel[h.channel_id].append(h)
    kept: list[Hit] = []
    for ch_hits in by_channel.values():
        kept.extend(drop_bursts(ch_hits, gap, tz))

    # 4. 全体をマージして古い順に
    kept.sort(key=lambda h: (h.created_at, h.id))
    print(f"連投除外後: {len(kept)}件")
    if not kept:
        return

    # 5. 「日付 リンク #チャンネル名」を整形して投稿
    names = await resolve_channel_names(client, {h.channel_id for h in kept})
    lines = [
        f"{h.created_at.astimezone(tz):%Y-%m-%d} "
        f"https://discord.com/channels/{settings.guild_id}/{h.channel_id}/{h.id} "
        f"#{names[h.channel_id]}"
        for h in kept
    ]

    if dry_run:
        print("\n".join(lines))
        return

    out = client.get_partial_messageable(settings.output_channel_id)
    for chunk in chunk_lines(lines, MSG_CHAR_LIMIT):
        await out.send(chunk, suppress_embeds=True)
        await asyncio.sleep(1)
    print("投稿しました。")


async def main() -> None:
    parser = argparse.ArgumentParser(description="写真付きメッセージのリンク一覧を投稿する")
    parser.add_argument("--min-images", type=int, help="画像N枚以上(.env.custom の MIN_IMAGES を上書き)")
    parser.add_argument("--gap-hours", type=float, help="連投とみなす時間差(.env.custom の GAP_HOURS を上書き)")
    parser.add_argument("--dry-run", action="store_true", help="投稿せず標準出力に表示する")
    args = parser.parse_args()

    settings = load_settings()
    min_images = args.min_images if args.min_images is not None else settings.min_images
    gap_hours = args.gap_hours if args.gap_hours is not None else settings.gap_hours

    # REST専用: login()のみでconnect()しない(ゲートウェイ非接続)
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(settings.token)
        await run(client, settings, min_images, gap_hours, args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
