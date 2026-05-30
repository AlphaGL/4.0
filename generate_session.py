#!/usr/bin/env python3
"""
Run this ONCE on your phone (Termux) or any machine where you can
receive a Telegram login code.

It will:
  1. Log in to Telegram with your phone number
  2. Save the session to uploader.session
  3. Print the base64 string you paste into GitHub Secrets as TELETHON_SESSION_B64

After this, you never need to log in again — GitHub uses the saved session.

Usage:
  pip install telethon
  python generate_session.py
"""

import base64
import os

API_ID   = 30550298                       # your TELETHON_API_ID
API_HASH = "6081410bff7e259d5db8ae01efb35309"  # your TELETHON_API_HASH
SESSION  = "uploader"

try:
    from telethon.sync import TelegramClient
except ImportError:
    print("❌  Install telethon first:  pip install telethon")
    raise SystemExit(1)

print("📱  Logging in to Telegram...")
print("    You will be asked for your phone number and the code Telegram sends you.\n")

with TelegramClient(SESSION, API_ID, API_HASH) as client:
    me = client.get_me()
    print(f"\n✅  Logged in as: {me.first_name} (@{me.username})\n")

session_file = f"{SESSION}.session"
if not os.path.exists(session_file):
    print(f"❌  Session file not found: {session_file}")
    raise SystemExit(1)

with open(session_file, "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

print("=" * 60)
print("COPY THE STRING BELOW — paste it into GitHub Secrets")
print("Secret name:  TELETHON_SESSION_B64")
print("=" * 60)
print()
print(b64)
print()
print("=" * 60)
print("✅  Done!  You can now push the workflow to GitHub.")
print("    The session is saved — no login needed again.")