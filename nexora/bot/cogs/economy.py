"""Economy: balances, daily/weekly/work rewards with streaks, payments, a role shop, inventory, achievements."""
from __future__ import annotations

import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from nexora.bot.helpers import manager_check
from nexora.core.utils import brand_embed, err, human_duration, ok, truncate

ACHIEVEMENTS = {
    "first_daily": ("Early Bird", "Claim your first daily reward"),
    "streak_7": ("Committed", "Reach a 7-day daily streak"),
    "streak_30": ("Unstoppable", "Reach a 30-day daily streak"),
    "rich_10k": ("Wealthy", "Hold 10,000 currency at once"),
    "rich_100k": ("Tycoon", "Hold 100,000 currency at once"),
    "first_purchase": ("Shopper", "Buy something from the shop"),
    "generous": ("Generous", "Pay another member"),
    "level_5": ("Getting Started", "Reach level 5"),
    "level_10": ("Regular", "Reach level 10"),
    "level_25": ("Veteran", "Reach level 25"),
    "level_50": ("Legend", "Reach level 50"),
    "level_100": ("Mythic", "Reach level 100"),
}
DAY = 86400


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def ensure(self, guild_id: int, user_id: int) -> dict:
        row = await self.bot.db.fetchone("SELECT * FROM economy WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        if row:
            return row
        s = await self.bot.settings.get(guild_id, "economy")
        await self.bot.db.execute("INSERT OR IGNORE INTO economy (guild_id, user_id, balance) VALUES (?, ?, ?)", (guild_id, user_id, s["start_balance"]))
        return await self.bot.db.fetchone("SELECT * FROM economy WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    async def add_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        await self.ensure(guild_id, user_id)
        await self.bot.db.execute(
            "UPDATE economy SET balance = balance + ?, total_earned = total_earned + MAX(?, 0) WHERE guild_id=? AND user_id=?",
            (amount, amount, guild_id, user_id))
        bal = await self.bot.db.fetchval("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (guild_id, user_id), 0)
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member:
            if bal >= 10_000:
                await self.grant_achievement(member, "rich_10k")
            if bal >= 100_000:
                await self.grant_achievement(member, "rich_100k")
        return bal

    async def try_spend(self, guild_id: int, user_id: int, amount: int) -> bool:
        await self.ensure(guild_id, user_id)
        n = await self.bot.db.execute_rowcount(
            "UPDATE economy SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?", (amount, guild_id, user_id, amount))
        return n == 1

    async def grant_achievement(self, member: discord.Member, key: str) -> bool:
        n = await self.bot.db.execute_rowcount("INSERT OR IGNORE INTO achievements (guild_id, user_id, key) VALUES (?, ?, ?)", (member.guild.id, member.id, key))
        return n == 1

    async def _enabled(self, interaction: discord.Interaction) -> dict | None:
        s = await self.bot.settings.get(interaction.guild_id, "economy")
        if not s["enabled"]:
            await interaction.response.send_message(embed=err("The economy is disabled on this server."), ephemeral=True)
            return None
        return s

    @staticmethod
    def money(s: dict, n: int) -> str:
        return f"{s['currency_symbol']} **{n:,}** {s['currency_name']}"

    @app_commands.command(description="Check your balance")
    @app_commands.guild_only()
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None):
        s = await self._enabled(interaction)
        if not s:
            return
        m = member or interaction.user
        row = await self.ensure(interaction.guild_id, m.id)
        embed = brand_embed(f"{m.display_name}'s wallet", self.money(s, row["balance"]))
        embed.add_field(name="Daily streak", value=f"{row['daily_streak']} days")
        embed.add_field(name="Total earned", value=f"{row['total_earned']:,}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Claim your daily reward")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction):
        s = await self._enabled(interaction)
        if not s:
            return
        row = await self.ensure(interaction.guild_id, interaction.user.id)
        now = time.time()
        if now - row["last_daily"] < DAY:
            return await interaction.response.send_message(embed=err(f"Come back in {human_duration(DAY - (now - row['last_daily']))}."), ephemeral=True)
        streak = row["daily_streak"] + 1 if now - row["last_daily"] < 2 * DAY else 1
        amount = s["daily_amount"] + s["daily_streak_bonus"] * (streak - 1)
        await self.bot.db.execute("UPDATE economy SET last_daily=?, daily_streak=? WHERE guild_id=? AND user_id=?", (now, streak, interaction.guild_id, interaction.user.id))
        bal = await self.add_balance(interaction.guild_id, interaction.user.id, amount)
        member = interaction.user
        await self.grant_achievement(member, "first_daily")
        if streak >= 7:
            await self.grant_achievement(member, "streak_7")
        if streak >= 30:
            await self.grant_achievement(member, "streak_30")
        await interaction.response.send_message(embed=ok(f"You claimed {self.money(s, amount)}! Streak: **{streak}** 🔥\nBalance: {bal:,}"))

    @app_commands.command(description="Claim your weekly reward")
    @app_commands.guild_only()
    async def weekly(self, interaction: discord.Interaction):
        s = await self._enabled(interaction)
        if not s:
            return
        row = await self.ensure(interaction.guild_id, interaction.user.id)
        now = time.time()
        if now - row["last_weekly"] < 7 * DAY:
            return await interaction.response.send_message(embed=err(f"Come back in {human_duration(7 * DAY - (now - row['last_weekly']))}."), ephemeral=True)
        await self.bot.db.execute("UPDATE economy SET last_weekly=? WHERE guild_id=? AND user_id=?", (now, interaction.guild_id, interaction.user.id))
        bal = await self.add_balance(interaction.guild_id, interaction.user.id, s["weekly_amount"])
        await interaction.response.send_message(embed=ok(f"You claimed {self.money(s, s['weekly_amount'])}! Balance: {bal:,}"))

    @app_commands.command(description="Work for some currency")
    @app_commands.guild_only()
    async def work(self, interaction: discord.Interaction):
        s = await self._enabled(interaction)
        if not s:
            return
        row = await self.ensure(interaction.guild_id, interaction.user.id)
        now, cd = time.time(), s["work_cooldown_minutes"] * 60
        if now - row["last_work"] < cd:
            return await interaction.response.send_message(embed=err(f"You're tired. Work again in {human_duration(cd - (now - row['last_work']))}."), ephemeral=True)
        lo, hi = sorted((s["work_min"], s["work_max"]))
        amount = random.randint(lo, hi)
        jobs = ["moderated a busy chat", "fixed a bot bug", "designed a server banner", "hosted an event", "answered support tickets", "streamed for the community"]
        await self.bot.db.execute("UPDATE economy SET last_work=? WHERE guild_id=? AND user_id=?", (now, interaction.guild_id, interaction.user.id))
        await self.add_balance(interaction.guild_id, interaction.user.id, amount)
        await interaction.response.send_message(embed=ok(f"You {random.choice(jobs)} and earned {self.money(s, amount)}."))

    @app_commands.command(description="Pay another member")
    @app_commands.guild_only()
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000_000]):
        s = await self._enabled(interaction)
        if not s:
            return
        if member.bot or member.id == interaction.user.id:
            return await interaction.response.send_message(embed=err("You can't pay that user."), ephemeral=True)
        if not await self.try_spend(interaction.guild_id, interaction.user.id, amount):
            return await interaction.response.send_message(embed=err("You don't have enough."), ephemeral=True)
        await self.add_balance(interaction.guild_id, member.id, amount)
        await self.grant_achievement(interaction.user, "generous")
        await interaction.response.send_message(embed=ok(f"You paid {member.mention} {self.money(s, amount)}."))

    @app_commands.command(description="Browse the server shop")
    @app_commands.guild_only()
    async def shop(self, interaction: discord.Interaction):
        s = await self._enabled(interaction)
        if not s:
            return
        items = await self.bot.db.fetchall("SELECT * FROM shop_items WHERE guild_id=? AND enabled=1 ORDER BY price", (interaction.guild_id,))
        embed = brand_embed("Shop", None if items else "The shop is empty. Admins can add items on the dashboard.")
        for it in items[:25]:
            stock = "∞" if it["stock"] < 0 else str(it["stock"])
            extra = f" · gives <@&{it['role_id']}>" if it["role_id"] else ""
            embed.add_field(name=f"#{it['id']} {it['name']} — {s['currency_symbol']} {it['price']:,}",
                            value=f"{truncate(it['description'], 200) or '—'}{extra} · stock {stock}", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Buy an item from the shop")
    @app_commands.guild_only()
    async def buy(self, interaction: discord.Interaction, item: int, quantity: app_commands.Range[int, 1, 100] = 1):
        s = await self._enabled(interaction)
        if not s:
            return
        it = await self.bot.db.fetchone("SELECT * FROM shop_items WHERE id=? AND guild_id=? AND enabled=1", (item, interaction.guild_id))
        if not it:
            return await interaction.response.send_message(embed=err("Item not found."), ephemeral=True)
        if it["role_id"]:
            quantity = 1
        if it["stock"] >= 0 and it["stock"] < quantity:
            return await interaction.response.send_message(embed=err("Not enough stock."), ephemeral=True)
        role = interaction.guild.get_role(it["role_id"]) if it["role_id"] else None
        if it["role_id"] and not role:
            return await interaction.response.send_message(embed=err("This item's role no longer exists."), ephemeral=True)
        if role and role in interaction.user.roles:
            return await interaction.response.send_message(embed=err("You already have that role."), ephemeral=True)
        cost = it["price"] * quantity
        if not await self.try_spend(interaction.guild_id, interaction.user.id, cost):
            return await interaction.response.send_message(embed=err(f"You need {self.money(s, cost)}."), ephemeral=True)
        if role:
            try:
                await interaction.user.add_roles(role, reason="Shop purchase")
            except discord.HTTPException:
                await self.add_balance(interaction.guild_id, interaction.user.id, cost)  # refund
                return await interaction.response.send_message(embed=err("I couldn't give you the role, so you were refunded."), ephemeral=True)
        if it["stock"] >= 0:
            await self.bot.db.execute("UPDATE shop_items SET stock = stock - ? WHERE id=?", (quantity, it["id"]))
        await self.bot.db.execute(
            """INSERT INTO inventory (guild_id, user_id, item_id, quantity) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET quantity = quantity + excluded.quantity""",
            (interaction.guild_id, interaction.user.id, it["id"], quantity))
        await self.grant_achievement(interaction.user, "first_purchase")
        await interaction.response.send_message(embed=ok(f"You bought **{quantity}× {it['name']}** for {self.money(s, cost)}."))

    @buy.autocomplete("item")
    async def _item_ac(self, interaction: discord.Interaction, current: str):
        rows = await self.bot.db.fetchall("SELECT id, name, price FROM shop_items WHERE guild_id=? AND enabled=1 ORDER BY price LIMIT 25", (interaction.guild_id,))
        return [app_commands.Choice(name=f"{r['name']} ({r['price']:,})", value=r["id"]) for r in rows if current.lower() in r["name"].lower()]

    @app_commands.command(description="Show your inventory")
    @app_commands.guild_only()
    async def inventory(self, interaction: discord.Interaction, member: discord.Member | None = None):
        m = member or interaction.user
        rows = await self.bot.db.fetchall(
            "SELECT i.quantity, s.name FROM inventory i JOIN shop_items s ON s.id=i.item_id WHERE i.guild_id=? AND i.user_id=? AND i.quantity > 0",
            (interaction.guild_id, m.id))
        await interaction.response.send_message(embed=brand_embed(f"{m.display_name}'s inventory", "\n".join(f"**{r['quantity']}×** {r['name']}" for r in rows) or "Empty."))

    @app_commands.command(description="Show achievements")
    @app_commands.guild_only()
    async def achievements(self, interaction: discord.Interaction, member: discord.Member | None = None):
        m = member or interaction.user
        have = {r["key"] for r in await self.bot.db.fetchall("SELECT key FROM achievements WHERE guild_id=? AND user_id=?", (interaction.guild_id, m.id))}
        lines = [f"{'🏆' if k in have else '🔒'} **{name}** — {desc}" for k, (name, desc) in ACHIEVEMENTS.items()]
        await interaction.response.send_message(embed=brand_embed(f"{m.display_name}'s achievements ({len(have)}/{len(ACHIEVEMENTS)})", "\n".join(lines)))

    eco = app_commands.Group(name="eco", description="Economy administration", guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @eco.command(name="give", description="Give currency to a member")
    @manager_check()
    async def eco_give(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000_000]):
        bal = await self.add_balance(interaction.guild_id, member.id, amount)
        await interaction.response.send_message(embed=ok(f"Gave {amount:,} to {member.mention}. New balance: {bal:,}"))

    @eco.command(name="take", description="Take currency from a member")
    @manager_check()
    async def eco_take(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000_000]):
        await self.ensure(interaction.guild_id, member.id)
        await self.bot.db.execute("UPDATE economy SET balance = MAX(0, balance - ?) WHERE guild_id=? AND user_id=?", (amount, interaction.guild_id, member.id))
        await interaction.response.send_message(embed=ok(f"Took up to {amount:,} from {member.mention}."))


async def setup(bot):
    await bot.add_cog(Economy(bot))
