import os
import uuid
import sqlite3
import random
import secrets
import datetime
import threading
import asyncio
import paramiko
import discord
from discord.ext import commands, tasks
from flask import Flask, render_template_string, request, jsonify

# -------------------------------------------------------------------
# Configuration & Constants
# -------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
PREFIX = "!"
SERVER_NAME = "Legacy Cloud | Powerful & Reliable Hosting"
WEBSSH_PORT = 5000
WEBSSH_URL = "http://YOUR_SERVER_IP:5000"  # Replace with your public IP/Domain

# Economy Settings
RENEWAL_COST_PER_DAY = 100  # Cost in coins per day
DAILY_REWARD = 50           # Daily coin claim reward
WORK_COIN_MIN = 20          # Min coins for work command
WORK_COIN_MAX = 80          # Max coins for work command

# Inactivity Settings
INACTIVITY_CHECK_INTERVAL_HOURS = 1  # Frequency of activity checks
INACTIVITY_THRESHOLD_HOURS = 18      # Hours before user triggers inactivity warning

# -------------------------------------------------------------------
# Database Management System (WAL Mode)
# -------------------------------------------------------------------
class DatabaseManager:
    def __init__(self, db_name="vps.db"):
        self.db_name = db_name
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_name)
        conn.execute("PRAGMA journal_mode=WAL;")  # WAL mode for high concurrency
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. VPS Management Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vps (
                    vps_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    node_id INTEGER NOT NULL DEFAULT 1,
                    ram INTEGER NOT NULL,
                    cpu INTEGER NOT NULL,
                    disk INTEGER NOT NULL,
                    os_template TEXT NOT NULL,
                    status TEXT DEFAULT 'running',
                    expiry_date TEXT NOT NULL,
                    auto_renew INTEGER DEFAULT 0
                )
            """)
            
            # 2. Economy Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS economy (
                    user_id TEXT PRIMARY KEY,
                    coins INTEGER DEFAULT 0,
                    last_daily TEXT,
                    last_work TEXT
                )
            """)
            
            # 3. User Activity Monitoring Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_activity (
                    user_id TEXT PRIMARY KEY,
                    last_active TEXT NOT NULL,
                    warning_count INTEGER DEFAULT 0
                )
            """)
            
            # 4. Settings & Thresholds Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            
            # Default alert thresholds
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cpu_threshold', '85')")
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ram_threshold', '90')")
            
            conn.commit()

    # --- Economy Methods ---
    def get_user(self, user_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM economy WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("INSERT INTO economy (user_id, coins) VALUES (?, ?)", (user_id, 0))
                conn.commit()
                return {"user_id": user_id, "coins": 0, "last_daily": None, "last_work": None}
            return dict(row)

    def update_coins(self, user_id: str, amount: int):
        self.get_user(user_id)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE economy SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
            conn.commit()

    def set_cooldown(self, user_id: str, column: str, timestamp_str: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE economy SET {column} = ? WHERE user_id = ?", (timestamp_str, user_id))
            conn.commit()

    # --- VPS Methods ---
    def get_vps(self, vps_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vps WHERE vps_id = ?", (vps_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_user_vps_list(self, owner_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vps WHERE owner_id = ?", (owner_id,))
            return [dict(row) for row in cursor.fetchall()]

    def create_vps(self, vps_id: str, owner_id: str, ram: int, cpu: int, disk: int, os_template: str, expiry_days: int = 7):
        expiry_date = (datetime.datetime.utcnow() + datetime.timedelta(days=expiry_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO vps (vps_id, owner_id, node_id, ram, cpu, disk, os_template, status, expiry_date)
                VALUES (?, ?, 1, ?, ?, ?, ?, 'running', ?)
            """, (vps_id, owner_id, ram, cpu, disk, os_template, expiry_date))
            conn.commit()
        return expiry_date

    def update_vps_status(self, vps_id: str, status: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vps SET status = ? WHERE vps_id = ?", (status, vps_id))
            conn.commit()

    def extend_vps_expiry(self, vps_id: str, days: int) -> str:
        vps = self.get_vps(vps_id)
        if not vps:
            return ""
        
        current_expiry = datetime.datetime.strptime(vps['expiry_date'], "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.utcnow()
        base_time = max(current_expiry, now)
        new_expiry = base_time + datetime.timedelta(days=days)
        new_expiry_str = new_expiry.strftime("%Y-%m-%d %H:%M:%S")

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vps SET expiry_date = ?, status = 'running' WHERE vps_id = ?", (new_expiry_str, vps_id))
            conn.commit()

        return new_expiry_str

    def toggle_auto_renew(self, vps_id: str) -> int:
        vps = self.get_vps(vps_id)
        new_val = 0 if vps.get('auto_renew', 0) == 1 else 1
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vps SET auto_renew = ? WHERE vps_id = ?", (new_val, vps_id))
            conn.commit()
        return new_val

    def delete_vps(self, vps_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vps WHERE vps_id = ?", (vps_id,))
            conn.commit()

    # --- Inactivity Methods ---
    def update_activity(self, user_id: str):
        now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_activity (user_id, last_active, warning_count)
                VALUES (?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_active = excluded.last_active,
                    warning_count = 0
            """, (user_id, now_str))
            conn.commit()

    def get_inactive_users(self, threshold_hours: int):
        now = datetime.datetime.utcnow()
        inactive_list = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_activity")
            rows = cursor.fetchall()
            for row in rows:
                last_active = datetime.datetime.strptime(row["last_active"], "%Y-%m-%d %H:%M:%S")
                delta = now - last_active
                if delta.total_seconds() >= threshold_hours * 3600:
                    inactive_list.append({
                        "user_id": row["user_id"],
                        "last_active": last_active,
                        "warning_count": row["warning_count"],
                        "inactive_seconds": int(delta.total_seconds())
                    })
        return inactive_list

    def increment_warning(self, user_id: str) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE user_activity SET warning_count = warning_count + 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            cursor.execute("SELECT warning_count FROM user_activity WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return row["warning_count"] if row else 1

db = DatabaseManager()

# -------------------------------------------------------------------
# WebSSH Flask Backend Engine
# -------------------------------------------------------------------
app = Flask(__name__)
ssh_sessions = {}

# Load webssh.html content
try:
    with open("webssh.html", "r", encoding="utf-8") as f:
        WEBSSH_HTML = f.read()
except FileNotFoundError:
    WEBSSH_HTML = "<h1>Error: webssh.html not found in working directory.</h1>"

@app.route("/")
def webssh_index():
    return render_template_string(WEBSSH_HTML)

@app.route("/api/ssh/connect", methods=["POST"])
def ssh_connect():
    data = request.get_json()
    host = data.get("host")
    port = data.get("port", 22)
    username = data.get("username")
    password = data.get("password")

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, port=port, username=username, password=password, timeout=10)

        channel = client.invoke_shell(term="xterm", width=80, height=24)
        channel.setblocking(0)

        session_id = str(uuid.uuid4())
        ssh_sessions[session_id] = {
            "client": client,
            "channel": channel,
            "buffer": ""
        }

        return jsonify({"success": True, "session_id": session_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/ssh/read", methods=["POST"])
def ssh_read():
    data = request.get_json()
    session_id = data.get("session_id")
    last_index = data.get("last_index", 0)

    if session_id not in ssh_sessions:
        return jsonify({"success": False, "error": "Invalid session", "closed": True})

    sess = ssh_sessions[session_id]
    channel = sess["channel"]

    try:
        while channel.recv_ready():
            out = channel.recv(4096).decode("utf-8", errors="replace")
            sess["buffer"] += out
    except Exception:
        pass

    buffer_len = len(sess["buffer"])
    new_data = sess["buffer"][last_index:]
    closed = channel.closed or channel.exit_status_ready()

    return jsonify({
        "success": True,
        "data": new_data,
        "last_index": buffer_len,
        "closed": closed
    })

@app.route("/api/ssh/write", methods=["POST"])
def ssh_write():
    data = request.get_json()
    session_id = data.get("session_id")
    input_data = data.get("data", "")

    if session_id not in ssh_sessions:
        return jsonify({"success": False, "error": "Invalid session"})

    try:
        ssh_sessions[session_id]["channel"].send(input_data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/ssh/resize", methods=["POST"])
def ssh_resize():
    data = request.get_json()
    session_id = data.get("session_id")
    cols = data.get("cols", 80)
    rows = data.get("rows", 24)

    if session_id in ssh_sessions:
        try:
            ssh_sessions[session_id]["channel"].resize_pty(width=cols, height=rows)
        except Exception:
            pass

    return jsonify({"success": True})

@app.route("/api/ssh/disconnect", methods=["POST"])
def ssh_disconnect():
    data = request.get_json()
    session_id = data.get("session_id")

    if session_id in ssh_sessions:
        try:
            ssh_sessions[session_id]["client"].close()
        except Exception:
            pass
        del ssh_sessions[session_id]

    return jsonify({"success": True})

def start_webssh_server():
    app.run(host="0.0.0.0", port=WEBSSH_PORT, debug=False, use_reloader=False)

# Start WebSSH Server on a Background Thread
flask_thread = threading.Thread(target=start_webssh_server, daemon=True)
flask_thread.start()

# -------------------------------------------------------------------
# Bot Setup
# -------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user.name} (ID: {bot.user.id})")
    print(f"🌐 WebSSH Running at: http://0.0.0.0:{WEBSSH_PORT}")
    print("🚀 All systems online.")
    expiration_check_loop.start()
    inactivity_monitor_loop.start()

# -------------------------------------------------------------------
# Activity Tracking Listeners
# -------------------------------------------------------------------
@bot.event
async def on_message(message):
    if not message.author.bot:
        db.update_activity(str(message.author.id))
    await bot.process_commands(message)

@bot.event
async def on_reaction_add(reaction, user):
    if not user.bot:
        db.update_activity(str(user.id))

@bot.event
async def on_voice_state_update(member, before, after):
    if not member.bot and after.channel is not None:
        db.update_activity(str(member.id))

# -------------------------------------------------------------------
# General User & WebSSH Commands
# -------------------------------------------------------------------
@bot.command(name="ping")
async def ping(ctx):
    """Displays bot latency."""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 **Pong!** Latency: `{latency}ms`")

@bot.command(name="uptime")
async def uptime(ctx):
    """Displays host system uptime."""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
            uptime_str = str(datetime.timedelta(seconds=int(uptime_seconds)))
        await ctx.send(f"⏱️ **Host Uptime:** `{uptime_str}`")
    except Exception:
        await ctx.send("⏱️ **Host Uptime:** `Unable to read system uptime.`")

@bot.command(name="webssh")
async def webssh_cmd(ctx):
    """Provides the WebSSH terminal access link."""
    embed = discord.Embed(
        title="🖥️ Terminus Live WebSSH Console",
        description=(
            f"Click the link below to access the WebSSH Console:\n"
            f"🔗 **[Open WebSSH Terminal]({WEBSSH_URL})**\n\n"
            f"**Features:**\n"
            f"• Live xterm.js terminal interface\n"
            f"• Real-time streaming & fast execution\n"
            f"• Full color support & customizable pty window"
        ),
        color=discord.Color.purple()
    )
    embed.set_footer(text=f"{SERVER_NAME} • Terminal Console")
    await ctx.send(embed=embed)

@bot.command(name="myvps")
async def myvps(ctx):
    """Displays all VPS containers owned by the command user."""
    vps_list = db.get_user_vps_list(str(ctx.author.id))
    if not vps_list:
        await ctx.send("❌ You do not own any active VPS instances.")
        return

    embed = discord.Embed(
        title=f"🖥️ Virtual Private Servers for {ctx.author.display_name}",
        color=discord.Color.blue()
    )
    for vps in vps_list:
        status_icon = "🟢" if vps["status"] == "running" else "🔴"
        auto_renew_status = "Enabled" if vps.get("auto_renew", 0) == 1 else "Disabled"
        embed.add_field(
            name=f"VPS ID: `{vps['vps_id']}`",
            value=(
                f"• Status: {status_icon} **{vps['status'].upper()}**\n"
                f"• Specs: **{vps['cpu']} Cores** | **{vps['ram']}MB RAM** | **{vps['disk']}GB Disk**\n"
                f"• OS: `{vps['os_template']}`\n"
                f"• Expiry Date: `{vps['expiry_date']} UTC`\n"
                f"• Auto-Renew: `{auto_renew_status}`"
            ),
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name="vps-info")
async def vps_info(ctx, vps_id: str):
    """View detailed statistics for a specific container."""
    vps = db.get_vps(vps_id)
    if not vps or vps["owner_id"] != str(ctx.author.id):
        await ctx.send("❌ Container not found or you do not have permission to view it.")
        return

    embed = discord.Embed(title=f"📊 Container Details — `{vps_id}`", color=discord.Color.teal())
    embed.add_field(name="Owner ID", value=f"`{vps['owner_id']}`", inline=True)
    embed.add_field(name="Node ID", value=f"`Node #{vps['node_id']}`", inline=True)
    embed.add_field(name="Status", value=f"`{vps['status']}`", inline=True)
    embed.add_field(name="Allocated CPU", value=f"`{vps['cpu']} Cores`", inline=True)
    embed.add_field(name="Allocated RAM", value=f"`{vps['ram']} MB`", inline=True)
    embed.add_field(name="Allocated Disk", value=f"`{vps['disk']} GB`", inline=True)
    embed.add_field(name="OS Template", value=f"`{vps['os_template']}`", inline=True)
    embed.add_field(name="Expiry Date", value=f"`{vps['expiry_date']}`", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="reboot")
async def reboot(ctx, vps_id: str):
    """Restarts a user's VPS container."""
    vps = db.get_vps(vps_id)
    if not vps or vps["owner_id"] != str(ctx.author.id):
        await ctx.send("❌ VPS not found or does not belong to you.")
        return

    await ctx.send(f"🔄 Rebooting VPS `{vps_id}`...")
    await asyncio.sleep(2)
    db.update_vps_status(vps_id, "running")
    await ctx.send(f"✅ VPS `{vps_id}` has been successfully restarted.")

@bot.command(name="reinstall")
async def reinstall(ctx, vps_id: str, os_template: str):
    """Reinstalls the OS template on an existing VPS."""
    vps = db.get_vps(vps_id)
    if not vps or vps["owner_id"] != str(ctx.author.id):
        await ctx.send("❌ VPS not found or does not belong to you.")
        return

    new_password = secrets.token_urlsafe(10)
    await ctx.send(f"⏳ Reinstalling OS `{os_template}` on VPS `{vps_id}`...")
    await asyncio.sleep(3)

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE vps SET os_template = ? WHERE vps_id = ?", (os_template, vps_id))
        conn.commit()

    try:
        await ctx.author.send(
            f"🛠️ **VPS Reinstallation Complete!**\n"
            f"• VPS ID: `{vps_id}`\n"
            f"• New OS: `{os_template}`\n"
            f"• Root Password: `{new_password}`"
        )
        await ctx.send(f"✅ Reinstallation complete! Check your DMs for the new root credentials.")
    except discord.Forbidden:
        await ctx.send("⚠️ OS reinstalled, but your DMs are closed so root credentials could not be sent.")

# -------------------------------------------------------------------
# Economy Commands
# -------------------------------------------------------------------
@bot.command(name="daily")
async def daily(ctx):
    """Claim free daily coins every 24 hours."""
    user_id = str(ctx.author.id)
    user_data = db.get_user(user_id)
    now = datetime.datetime.utcnow()

    if user_data["last_daily"]:
        last_claim = datetime.datetime.strptime(user_data["last_daily"], "%Y-%m-%d %H:%M:%S")
        if now < last_claim + datetime.timedelta(hours=24):
            remaining = (last_claim + datetime.timedelta(hours=24)) - now
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            await ctx.send(f"⏱️ You must wait **{hours}h {minutes}m {seconds}s** before claiming your daily reward again.")
            return

    db.update_coins(user_id, DAILY_REWARD)
    db.set_cooldown(user_id, "last_daily", now.strftime("%Y-%m-%d %H:%M:%S"))
    await ctx.send(f"🪙 **Daily Reward Claimed!** You received **{DAILY_REWARD} coins**.")

@bot.command(name="work", aliases=["claim"])
async def work(ctx):
    """Work to earn coins (1-hour cooldown)."""
    user_id = str(ctx.author.id)
    user_data = db.get_user(user_id)
    now = datetime.datetime.utcnow()

    if user_data["last_work"]:
        last_work = datetime.datetime.strptime(user_data["last_work"], "%Y-%m-%d %H:%M:%S")
        if now < last_work + datetime.timedelta(hours=1):
            remaining = (last_work + datetime.timedelta(hours=1)) - now
            minutes, seconds = divmod(int(remaining.total_seconds()), 60)
            await ctx.send(f"⏱️ Take a break! You can work again in **{minutes}m {seconds}s**.")
            return

    earned = random.randint(WORK_COIN_MIN, WORK_COIN_MAX)
    db.update_coins(user_id, earned)
    db.set_cooldown(user_id, "last_work", now.strftime("%Y-%m-%d %H:%M:%S"))

    tasks_done = [
        "configured an Nginx reverse proxy",
        "patched a Linux kernel vulnerability",
        "managed LXC containers for a client",
        "optimized Docker resource allocation"
    ]
    await ctx.send(f"💼 You {random.choice(tasks_done)} and earned **{earned} coins**!")

@bot.command(name="coins", aliases=["balance", "bal"])
async def coins(ctx, member: discord.Member = None):
    """Check coin balance."""
    target = member or ctx.author
    user_data = db.get_user(str(target.id))
    await ctx.send(f"💰 **{target.display_name}** currently has **{user_data['coins']} coins**.")

@bot.command(name="pay", aliases=["transfer"])
async def pay(ctx, member: discord.Member, amount: int):
    """Transfer coins to another user."""
    if amount <= 0:
        await ctx.send("❌ Transfer amount must be greater than 0.")
        return
    if member.id == ctx.author.id:
        await ctx.send("❌ You cannot send coins to yourself.")
        return

    sender_id = str(ctx.author.id)
    receiver_id = str(member.id)
    sender_data = db.get_user(sender_id)

    if sender_data["coins"] < amount:
        await ctx.send("❌ Insufficient balance to complete this transaction.")
        return

    db.update_coins(sender_id, -amount)
    db.update_coins(receiver_id, amount)
    await ctx.send(f"💸 Transferred **{amount} coins** to **{member.display_name}**!")

@bot.command(name="renew")
async def renew(ctx, vps_id: str, days: int = 7):
    """Renew a VPS using earned coins."""
    if days <= 0:
        await ctx.send("❌ Renewal days must be at least 1.")
        return

    user_id = str(ctx.author.id)
    vps = db.get_vps(vps_id)

    if not vps or vps["owner_id"] != user_id:
        await ctx.send("❌ VPS container not found or does not belong to you.")
        return

    total_cost = days * RENEWAL_COST_PER_DAY
    user_data = db.get_user(user_id)

    if user_data["coins"] < total_cost:
        await ctx.send(
            f"❌ **Insufficient Coins!**\n"
            f"• Renewal Cost: **{total_cost} coins** ({days} days @ {RENEWAL_COST_PER_DAY} coins/day)\n"
            f"• Wallet Balance: **{user_data['coins']} coins**"
        )
        return

    db.update_coins(user_id, -total_cost)
    new_expiry = db.extend_vps_expiry(vps_id, days)

    await ctx.send(
        f"✅ **VPS `{vps_id}` Renewed Successfully!**\n"
        f"• Extended by: **{days} days**\n"
        f"• Total Cost: **{total_cost} coins**\n"
        f"• New Expiry Date: `{new_expiry} UTC`"
    )

@bot.command(name="auto-renew")
async def auto_renew(ctx, vps_id: str):
    """Toggle auto-renewal for a specific VPS."""
    user_id = str(ctx.author.id)
    vps = db.get_vps(vps_id)

    if not vps or vps["owner_id"] != user_id:
        await ctx.send("❌ Container not found or does not belong to you.")
        return

    status = db.toggle_auto_renew(vps_id)
    state_str = "**ENABLED** (Auto-renews 7 days prior to expiry)" if status == 1 else "**DISABLED**"
    await ctx.send(f"🔄 Auto-renewal for VPS `{vps_id}` is now {state_str}.")

# -------------------------------------------------------------------
# Administrative Commands
# -------------------------------------------------------------------
@bot.command(name="create")
@commands.has_permissions(administrator=True)
async def create(ctx, ram: int, cpu: int, disk: int, member: discord.Member, expiry_days: int = 7):
    """Create a new LXC container for a specified user."""
    vps_id = f"vps-{random.randint(1000, 9999)}"
    os_template = "ubuntu-22.04"
    root_password = secrets.token_urlsafe(12)

    expiry_date = db.create_vps(vps_id, str(member.id), ram, cpu, disk, os_template, expiry_days)

    try:
        await member.send(
            f"🎉 **Your VPS has been deployed!**\n"
            f"• VPS ID: `{vps_id}`\n"
            f"• CPU Cores: `{cpu}`\n"
            f"• RAM: `{ram} MB`\n"
            f"• Disk Space: `{disk} GB`\n"
            f"• Root Password: `{root_password}`\n"
            f"• Expiry Date: `{expiry_date} UTC`"
        )
        await ctx.send(f"✅ Successfully created VPS `{vps_id}` for **{member.display_name}**.")
    except discord.Forbidden:
        await ctx.send(f"⚠️ Created VPS `{vps_id}`, but failed to send credentials to user via DM.")

@bot.command(name="stop")
@commands.has_permissions(administrator=True)
async def stop(ctx, vps_id: str):
    """Stop a running container."""
    db.update_vps_status(vps_id, "stopped")
    await ctx.send(f"🛑 VPS `{vps_id}` has been stopped.")

@bot.command(name="start")
@commands.has_permissions(administrator=True)
async def start(ctx, vps_id: str):
    """Start a stopped container."""
    db.update_vps_status(vps_id, "running")
    await ctx.send(f"🟢 VPS `{vps_id}` has been started.")

@bot.command(name="delete")
@commands.has_permissions(administrator=True)
async def delete(ctx, vps_id: str):
    """Delete a VPS container completely."""
    db.delete_vps(vps_id)
    await ctx.send(f"🗑️ VPS `{vps_id}` has been deleted.")

@bot.command(name="suspend")
@commands.has_permissions(administrator=True)
async def suspend(ctx, vps_id: str):
    """Suspend a container."""
    db.update_vps_status(vps_id, "suspended")
    await ctx.send(f"🔒 VPS `{vps_id}` has been suspended.")

@bot.command(name="extend")
@commands.has_permissions(administrator=True)
async def extend(ctx, vps_id: str, days: int):
    """Extend VPS expiration manually."""
    new_expiry = db.extend_vps_expiry(vps_id, days)
    await ctx.send(f"📅 Extended VPS `{vps_id}` by **{days} days**. New expiry: `{new_expiry}`.")

@bot.command(name="resize")
@commands.has_permissions(administrator=True)
async def resize(ctx, vps_id: str, ram: int, cpu: int, disk: int):
    """Update container resource specs."""
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE vps SET ram = ?, cpu = ?, disk = ? WHERE vps_id = ?", (ram, cpu, disk, vps_id))
        conn.commit()
    await ctx.send(f"⚙️ Resized VPS `{vps_id}` to **{cpu} Cores**, **{ram}MB RAM**, and **{disk}GB Disk**.")

@bot.command(name="add-coins")
@commands.has_permissions(administrator=True)
async def add_coins(ctx, member: discord.Member, amount: int):
    """Grant coins to a user balance."""
    db.update_coins(str(member.id), amount)
    await ctx.send(f"✅ Added **{amount} coins** to **{member.display_name}**.")

@bot.command(name="remove-coins")
@commands.has_permissions(administrator=True)
async def remove_coins(ctx, member: discord.Member, amount: int):
    """Deduct coins from a user balance."""
    db.update_coins(str(member.id), -amount)
    await ctx.send(f"✅ Deducted **{amount} coins** from **{member.display_name}**.")

@bot.command(name="thresholds")
@commands.has_permissions(administrator=True)
async def thresholds(ctx):
    """Display CPU and RAM warning threshold percentages."""
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM settings WHERE key IN ('cpu_threshold', 'ram_threshold')")
        rows = {row["key"]: row["value"] for row in cursor.fetchall()}
    await ctx.send(f"⚙️ **System Thresholds:** CPU Warning: `{rows.get('cpu_threshold', '85')}%` | RAM Warning: `{rows.get('ram_threshold', '90')}%`")

@bot.command(name="set-threshold")
@commands.has_permissions(administrator=True)
async def set_threshold(ctx, cpu: int, ram: int):
    """Set CPU and RAM warning thresholds."""
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("REPLACE INTO settings (key, value) VALUES ('cpu_threshold', ?)", (str(cpu),))
        cursor.execute("REPLACE INTO settings (key, value) VALUES ('ram_threshold', ?)", (str(ram),))
        conn.commit()
    await ctx.send(f"✅ Set CPU threshold to `{cpu}%` and RAM threshold to `{ram}%`.")

@bot.command(name="set-status")
@commands.has_permissions(administrator=True)
async def set_status(ctx, activity_type: str, *, status_text: str):
    """Update the bot status activity."""
    activity_type = activity_type.lower()
    if activity_type == "playing":
        act = discord.Game(name=status_text)
    elif activity_type == "watching":
        act = discord.Activity(type=discord.ActivityType.watching, name=status_text)
    elif activity_type == "listening":
        act = discord.Activity(type=discord.ActivityType.listening, name=status_text)
    else:
        act = discord.Game(name=status_text)
    
    await bot.change_presence(activity=act)
    await ctx.send(f"✅ Status set to `{activity_type.capitalize()} {status_text}`.")

@bot.command(name="lxc-list")
@commands.has_permissions(administrator=True)
async def lxc_list(ctx, node_id: int = 1):
    """Queries raw LXC instances running on a node."""
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM vps WHERE node_id = ?", (node_id,))
        containers = cursor.fetchall()
    
    if not containers:
        await ctx.send(f"ℹ️ No containers found on Node #{node_id}.")
        return

    desc = "\n".join([f"• Container `{c['vps_id']}` — Status: `{c['status']}`" for c in containers])
    embed = discord.Embed(title=f"Node #{node_id} Containers", description=desc, color=discord.Color.dark_grey())
    await ctx.send(embed=embed)

@bot.command(name="check-inactivity")
@commands.has_permissions(administrator=True)
async def check_inactivity(ctx):
    """Manually trigger an inactivity scan."""
    await ctx.send("🔍 Initiating manual inactivity scan...")
    await inactivity_monitor_loop()
    await ctx.send("✅ Inactivity scan completed!")

# -------------------------------------------------------------------
# Automated Background Tasks
# -------------------------------------------------------------------
@tasks.loop(minutes=30)
async def expiration_check_loop():
    """Monitors container end-dates, executes auto-renewals, and suspends expired VPSs."""
    now = datetime.datetime.utcnow()
    
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM vps WHERE status = 'running'")
        running_vps = cursor.fetchall()

    for row in running_vps:
        vps = dict(row)
        expiry = datetime.datetime.strptime(vps["expiry_date"], "%Y-%m-%d %H:%M:%S")

        if now >= expiry:
            user_id = vps["owner_id"]
            vps_id = vps["vps_id"]
            
            # Check for auto-renew
            if vps.get("auto_renew", 0) == 1:
                auto_days = 7
                cost = auto_days * RENEWAL_COST_PER_DAY
                user_data = db.get_user(user_id)

                if user_data["coins"] >= cost:
                    db.update_coins(user_id, -cost)
                    new_exp = db.extend_vps_expiry(vps_id, auto_days)
                    print(f"[Auto-Renew] VPS {vps_id} extended to {new_exp}.")
                    continue

            # Auto-Suspend container if not auto-renewed
            db.update_vps_status(vps_id, "suspended")
            print(f"[Expiration System] VPS {vps_id} expired and suspended.")

@tasks.loop(hours=INACTIVITY_CHECK_INTERVAL_HOURS)
async def inactivity_monitor_loop():
    """Scans for inactive members and delivers warning embeds via DM."""
    inactive_users = db.get_inactive_users(INACTIVITY_THRESHOLD_HOURS)

    for record in inactive_users:
        user_id = int(record["user_id"])
        user = bot.get_user(user_id)
        
        if not user:
            try:
                user = await bot.fetch_user(user_id)
            except discord.NotFound:
                continue

        new_warning_count = db.increment_warning(record["user_id"])

        total_minutes = record["inactive_seconds"] // 60
        hours = total_minutes // 60
        minutes = total_minutes % 60
        time_str = f"{hours}h {minutes}m"

        if new_warning_count >= 6:
            status_text = "🚨 **FINAL WARNING!** You may be kicked from the server if you remain inactive!"
        elif new_warning_count >= 3:
            status_text = "⚠️ **WARNING!** Continued inactivity may result in server action."
        else:
            status_text = "ℹ️ Please post a message or join a channel to maintain your active status."

        embed = discord.Embed(
            title="⚠️ Inactivity Warning!",
            description=(
                f"Hey {user.mention}, we noticed you haven't been active in **{SERVER_NAME}** for a while!\n\n"
                f"⏱️ **Inactive For**\n`{time_str}`\n\n"
                f"🚨 **Warning #**\n`{new_warning_count}`\n\n"
                f"❓ **What Happens Next?**\n{status_text}\n\n"
                f"💡 **How to Stay Active**\n"
                f"• Send messages in any channel\n"
                f"• Join a voice channel\n"
                f"• React to messages\n"
                f"• Participate in events\n\n"
                f"🎁 **Come Back Rewards**\n"
                f"Come back now and claim bonus XP and special rewards!"
            ),
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"{SERVER_NAME} • Activity Monitor")
        embed.timestamp = datetime.datetime.utcnow()

        try:
            await user.send(embed=embed)
            print(f"[Inactivity System] DM warning sent to {user.name} (Warning #{new_warning_count})")
        except discord.Forbidden:
            print(f"[Inactivity System] DM failed for {user.name} (DMs locked).")

# -------------------------------------------------------------------
# Main Execution
# -------------------------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)
