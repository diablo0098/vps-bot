# 🖥️ Legacy Cloud Deploy Bot V1 — Discord VPS & LXC Management Bot

A full-featured Discord bot for managing Virtual Private Servers (VPS) and LXC containers, featuring an integrated **WebSSH terminal** (xterm.js), an internal **economy system**, **automated inactivity monitoring**, and **auto-renewal workflows**. Built with `discord.py`, `Flask`, `Paramiko`, and SQLite in WAL mode.

---

## ✨ Features

- 🖥️ **Live WebSSH Terminal**: Built-in Flask server streaming interactive xterm.js SSH terminal sessions.
- ⚙️ **VPS Container Control**: Start, stop, reboot, reinstall, and resize LXC containers directly from Discord.
- 🪙 **Virtual Economy**: Users can work, claim daily coin rewards, and pay each other to earn currency for server renewals.
- 🔄 **Auto & Manual Renewals**: Automatic container extension using user coin balances before expiry.
- 🚨 **Inactivity Tracking**: Monitors user activity (messages, reactions, voice) and sends automatic DM warnings to inactive users.
- ⚡ **High-Performance Database**: SQLite configured with Write-Ahead Logging (WAL) for high concurrency.

---

## 📋 Prerequisites

Before running the bot, ensure you have:

- **Python 3.10+** installed on your host system.
- A **Discord Bot Token** with `Message Content`, `Server Members`, and `Presence` Privileged Gateway Intents enabled in the [Discord Developer Portal](https://discord.com/developers/applications).
- Port `5000` (or your configured port) open on your firewall for the WebSSH interface.

---

## 🚀 Installation & Setup

### 1. Clone the Repository

```bash
git clone https://github.com/diablo0098/vps-bot
cd vps-bot


apt install pip -y


pip install -r requirements.txt

cp .env.example .env


ls -la webssh.html bot.py


python bot.py
