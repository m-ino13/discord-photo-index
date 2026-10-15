#!/usr/bin/env python3
"""検索APIが使えない原因(403 Missing Access など)を切り分けるための診断スクリプト。

使い方:
    python tools/diag_search.py              # .env.custom の GUILD_ID を使う
    python tools/diag_search.py <GUILD_ID>   # 別のサーバーIDを指定する

トークンはリポジトリ直下の .env から読み込む(環境変数は使わない)。
"""
from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path

import discord
from discord.http import Route
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent
SECRETS_FILE = BASE_DIR / ".env"
SETTINGS_FILE = BASE_DIR / ".env.custom"

_step = itertools.count(1)


def section(title: str) -> None:
    """見出しを「[1] ...」「[2] ...」と連番で表示する(手順を増減しても番号がずれない)。"""
    print(f"\n[{next(_step)}] {title}")


def load_config() -> tuple[str, int]:
    """(トークン, サーバーID) を返す。サーバーIDは、引数があれば引数、無ければ .env.custom から。"""
    if not SECRETS_FILE.exists():
        sys.exit(f"設定ファイルが見つかりません: {SECRETS_FILE}")
    token = (dotenv_values(SECRETS_FILE).get("DISCORD_TOKEN") or "").strip()
    if not token:
        sys.exit(f"{SECRETS_FILE.name} に DISCORD_TOKEN を設定してください。")

    if len(sys.argv) > 1:
        raw = sys.argv[1]
    else:
        if not SETTINGS_FILE.exists():
            sys.exit(f"設定ファイルが見つかりません: {SETTINGS_FILE}")
        raw = (dotenv_values(SETTINGS_FILE).get("GUILD_ID") or "").strip()
    try:
        return token, int(raw)
    except ValueError:
        sys.exit(f"GUILD_ID を指定してください(引数、または {SETTINGS_FILE.name} の GUILD_ID)。")


async def main(token: str, guild_id: int) -> None:
    async with discord.Client(intents=discord.Intents.none()) as client:
        section("ログイン")
        await client.login(token)
        print("    OK (トークンは有効)")

        section("Botの参加サーバー")
        guilds = [g async for g in client.fetch_guilds(limit=200)]
        print(f"    {len(guilds)}件")
        for g in guilds:
            mark = "   <-- 対象のGUILD_ID" if g.id == guild_id else ""
            print(f"    {g.id}  {g.name}{mark}")
        if guild_id not in {g.id for g in guilds}:
            print("    !! 対象のGUILD_IDがBotの参加サーバーに無い -> ID違い or 未参加")

        section("チャンネル一覧  ※Botが見えないチャンネルも含めて返ってくる")
        guild = None
        chans = []
        try:
            guild = await client.fetch_guild(guild_id)
            chans = await guild.fetch_channels()
            print(f"    OK ({len(chans)}件)")
        except discord.HTTPException as e:
            print(f"    NG status={e.status} code={e.code}")

        section("Botのロール権限(サーバー全体)")
        me = None
        if guild is None:
            print("    (サーバー情報を取得できなかったので、スキップ)")
        else:
            try:
                me = await guild.fetch_member(client.user.id)
                gp = me.guild_permissions
                print(
                    f"    管理者={gp.administrator} / チャンネルを見る={gp.view_channel} "
                    f"/ メッセージ履歴を読む={gp.read_message_history}"
                )
            except discord.HTTPException as e:
                print(f"    メンバー情報を取得できません: status={e.status} code={e.code}")

        # 「メッセージ履歴を読む」権限が無い場合、履歴取得はエラーにならず0件が返る(公式ドキュメント)
        section("チャンネルごとの履歴読み取り  ※書式: 名前 | 履歴取得 | 実効権限(見る/履歴を読む)")
        text_chans = [c for c in chans if isinstance(c, discord.TextChannel)]
        readable_ids = []
        for c in text_chans:
            route = Route("GET", "/channels/{channel_id}/messages", channel_id=c.id)
            perm = ""
            if me is not None:
                p = c.permissions_for(me)
                perm = f" | 見る={p.view_channel} 履歴={p.read_message_history}"
            try:
                msgs = await client.http.request(route, params=[("limit", 1)])
                readable_ids.append(c.id)
                print(f"    {c.name} | OK ({len(msgs)}件返却){perm}")
            except discord.HTTPException as e:
                print(f"    {c.name} | NG code={e.code}{perm}")

        section("検索API")
        route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=guild_id)
        tests = [
            ("has無し", [("limit", 1)]),
            ("has=image", [("limit", 1), ("has", "image")]),
        ]
        # 読めるチャンネルだけに絞った検索(channel_idは、同じキーを繰り返して配列にする)
        if readable_ids:
            image_query = [("limit", 1), ("has", "image")]
            tests.append(
                ("読めるチャンネル全部を指定", image_query + [("channel_id", i) for i in readable_ids])
            )
            tests.append(
                ("読めるチャンネル1つだけ指定", image_query + [("channel_id", readable_ids[0])])
            )
        for label, params in tests:
            try:
                data = await client.http.request(route, params=params)
                print(f"    {label}: OK keys={list(data)}")
            except discord.HTTPException as e:
                print(f"    {label}: NG status={e.status} code={e.code} {e.text}")


if __name__ == "__main__":
    token, guild_id = load_config()
    asyncio.run(main(token, guild_id))
