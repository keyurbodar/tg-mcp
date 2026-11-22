import argparse
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    parser = argparse.ArgumentParser(description="Log in to Telegram once and print a session string.")
    parser.add_argument("--out", help="write the session string to this file instead of printing it")
    args = parser.parse_args()
    api_id = int(os.environ.get("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: "))
    api_hash = os.environ.get("TELEGRAM_API_HASH") or input("TELEGRAM_API_HASH: ")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.start()
        session = client.session.save()
        me = await client.get_me()
    if args.out:
        with open(args.out, "w") as f:
            f.write(session)
        os.chmod(args.out, 0o600)
        print(f"logged in as {me.first_name} ({me.id}); session written to {args.out}")
    else:
        print(f"logged in as {me.first_name} ({me.id})\nTELEGRAM_SESSION_STRING={session}")


if __name__ == "__main__":
    asyncio.run(main())
