# ⚡ Legacy Cloud Proxmox VPS Bot

A feature-rich Discord bot and web control panel designed for Proxmox VE VPS deployment, internal coin economy (LC), remote terminal access (sshx & tmate), automated backups, and activity logging.

**Developed by devaru007 & Legacy Cloud**

---

## 🌟 Key Features

* **Member VPS Limits:** Members can deploy LXC VPS instances (`10GB RAM`, `20GB Disk`, `2 vCPU`) via `/deploy`, which expire automatically after **10 Days**.
* **Automated Expiration, Backup & DM:** When a 10-day member VPS expires, the system automatically creates a full backup, purges the instance from Proxmox, stores the backup in the web dashboard, and sends a DM to the user:
  > *"Your VPS has been deleted because 10 days have passed. If you want your backup, please talk to an admin."*
* **Admin Unlimited Grants:** Admins have complete control to provision **LXC** (`/admin-give-vps`) or **Dedicated KVM Virtual Machines** (`/admin-give-kvm`) with **custom RAM, Disk, and CPU specs** with **Lifetime** duration.
* **Remote Terminal Access:** Built-in support to instantly generate **sshx** and **tmate** web terminal session links for easy SSH access.
* **Web Control Dashboard:** Integrated Flask dashboard running locally on `http://127.0.0.1:3001` (with Cloudflare HTTPS tunnel) to manage user LC balances, trigger manual backups, inspect stored expired backups, deploy KVMs, and review audit logs.

---

## 🛠️ Installation & Setup Guide

### Step 1: Clone the Repository

```bash
git clone https://github.com/diablo0098/vps-bot.git

cd vps-bot

Step 2: Install Dependencies
Install all necessary packages using pip:

```bash
pip install -r requirements.txt

Step 3: Configure Environment Variables
Create a .env file in the root directory by copying or creating a new one:

```bash
cp .env.example .env

```bash
nano .env 

```bash
python bot.py
