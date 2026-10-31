"""Role management: button / dropdown / reaction role menus, temporary roles, role commands."""
from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from nexora.bot.helpers import log_event
from nexora.core.utils import brand_embed, err, human_duration, ok, parse_duration, truncate
from nexora.db import jload, to_db_time, utcnow


async def apply_menu_choice(member: discord.Member, menu: dict, selected: set[int]) -> tuple[list[discord.Role], list[discord.Role]]:
    """Make the member's roles from this menu equal `selected`. Returns (added, removed)."""
    guild = member.guild
    menu_roles = {int(o["role_id"]) for o in jload(menu["options"], []) if o.get("role_id")}
    me_top = guild.me.top_role
    add = [r for r in (guild.get_role(i) for i in selected - {r.id for r in member.roles}) if r and r < me_top]
    remove = [r for r in member.roles if r.id in menu_roles and r.id not in selected and r < me_top]
    if add:
        await member.add_roles(*add, reason=f"Role menu #{menu['id']}")
    if remove:
        await member.remove_roles(*remove, reason=f"Role menu #{menu['id']}")
    return add, remove


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:rb:(?P<menu>\d+):(?P<role>\d+)"):
    def __init__(self, menu_id: int, role_id: int, label: str = "Role", emoji: str | None = None):
        super().__init__(discord.ui.Button(label=label, emoji=emoji or None, style=discord.ButtonStyle.secondary, custom_id=f"nx:rb:{menu_id}:{role_id}"))
        self.menu_id, self.role_id = menu_id, role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["menu"]), int(match["role"]))

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db  # type: ignore[attr-defined]
        menu = await db.fetchone("SELECT * FROM role_menus WHERE id=? AND guild_id=?", (self.menu_id, interaction.guild_id))
        member = interaction.user
        if not menu or not isinstance(member, discord.Member):
            return await interaction.response.send_message(embed=err("This menu no longer exists."), ephemeral=True)
        current = {r.id for r in member.roles} & {int(o["role_id"]) for o in jload(menu["options"], [])}
        if self.role_id in current:
            selected = current - {self.role_id}
        elif menu["max_values"] == 1:
            selected = {self.role_id}
        else:
            if menu["max_values"] and len(current) >= menu["max_values"]:
                return await interaction.response.send_message(embed=err(f"You can only pick {menu['max_values']} roles from this menu."), ephemeral=True)
            selected = current | {self.role_id}
        try:
            add, rem = await apply_menu_choice(member, menu, selected)
        except discord.HTTPException:
            return await interaction.response.send_message(embed=err("I couldn't change your roles. My role must be above them."), ephemeral=True)
        parts = ([f"Added {' '.join(r.mention for r in add)}"] if add else []) + ([f"Removed {' '.join(r.mention for r in rem)}"] if rem else [])
        await interaction.response.send_message(embed=ok(" · ".join(parts) or "No change."), ephemeral=True)


class RoleSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"nx:rs:(?P<menu>\d+)"):
    def __init__(self, menu_id: int, options: list[discord.SelectOption] | None = None, max_values: int = 1, placeholder: str = "Choose roles"):
        options = options or [discord.SelectOption(label="-", value="0")]
        super().__init__(discord.ui.Select(custom_id=f"nx:rs:{menu_id}", options=options, min_values=0,
                                           max_values=max(1, min(max_values or len(options), len(options))), placeholder=placeholder))
        self.menu_id = menu_id

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Select, match: re.Match[str]):
        inst = cls(int(match["menu"]), item.options, item.max_values, item.placeholder or "Choose roles")
        return inst

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db  # type: ignore[attr-defined]
        menu = await db.fetchone("SELECT * FROM role_menus WHERE id=? AND guild_id=?", (self.menu_id, interaction.guild_id))
        if not menu or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(embed=err("This menu no longer exists."), ephemeral=True)
        selected = {int(v) for v in interaction.data.get("values", [])}  # type: ignore[union-attr]
        try:
            add, rem = await apply_menu_choice(interaction.user, menu, selected)
        except discord.HTTPException:
            return await interaction.response.send_message(embed=err("I couldn't change your roles."), ephemeral=True)
        parts = ([f"Added {' '.join(r.mention for r in add)}"] if add else []) + ([f"Removed {' '.join(r.mention for r in rem)}"] if rem else [])
        await interaction.response.send_message(embed=ok(" · ".join(parts) or "No change."), ephemeral=True)


class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_dynamic_items(RoleButton, RoleSelect)
        self.temp_loop.start()

    def cog_unload(self):
        self.temp_loop.cancel()

    async def publish_menu(self, menu_id: int) -> discord.Message:
        menu = await self.bot.db.fetchone("SELECT * FROM role_menus WHERE id=?", (menu_id,))
        if not menu:
            raise ValueError("Menu not found")
        guild = self.bot.get_guild(menu["guild_id"])
        ch = guild.get_channel(menu["channel_id"] or 0) if guild else None
        if not isinstance(ch, discord.TextChannel):
            raise ValueError("Menu channel not set or not found")
        options = [o for o in jload(menu["options"], []) if o.get("role_id") and guild.get_role(int(o["role_id"]))][:25]
        if not options:
            raise ValueError("Add at least one existing role to the menu")
        desc = menu["description"] or ""
        if menu["kind"] == "reaction":
            desc += "\n\n" + "\n".join(f"{o.get('emoji') or '•'} — <@&{o['role_id']}>" + (f" · {o['description']}" if o.get("description") else "") for o in options)
        embed = brand_embed(menu["title"], desc.strip() or None)
        view = None
        if menu["kind"] == "button":
            view = discord.ui.View(timeout=None)
            for o in options:
                view.add_item(RoleButton(menu_id, int(o["role_id"]), truncate(o.get("label") or guild.get_role(int(o["role_id"])).name, 80), o.get("emoji") or None))
        elif menu["kind"] == "dropdown":
            view = discord.ui.View(timeout=None)
            opts = [discord.SelectOption(label=truncate(o.get("label") or guild.get_role(int(o["role_id"])).name, 100), value=str(o["role_id"]),
                                         emoji=o.get("emoji") or None, description=truncate(o.get("description"), 100) or None) for o in options]
            view.add_item(RoleSelect(menu_id, opts, menu["max_values"] or len(opts), truncate(menu["title"], 150)))
        msg = None
        if menu["message_id"]:
            try:
                msg = await ch.fetch_message(menu["message_id"])
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException:
                msg = None
        if msg is None:
            msg = await ch.send(embed=embed, view=view)
            await self.bot.db.execute("UPDATE role_menus SET message_id=? WHERE id=?", (msg.id, menu_id))
        if menu["kind"] == "reaction":
            for o in options:
                if o.get("emoji"):
                    try:
                        await msg.add_reaction(o["emoji"])
                    except discord.HTTPException:
                        pass
        return msg

    async def _reaction_menu(self, payload: discord.RawReactionActionEvent) -> tuple[dict, int] | None:
        if not payload.guild_id or payload.user_id == self.bot.user.id:
            return None
        menu = await self.bot.db.fetchone("SELECT * FROM role_menus WHERE message_id=? AND kind='reaction'", (payload.message_id,))
        if not menu:
            return None
        emoji = str(payload.emoji)
        for o in jload(menu["options"], []):
            if o.get("emoji") and (o["emoji"] == emoji or o["emoji"] == payload.emoji.name):
                return menu, int(o["role_id"])
        return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        found = await self._reaction_menu(payload)
        if not found or not isinstance(payload.member, discord.Member) or payload.member.bot:
            return
        menu, role_id = found
        current = {r.id for r in payload.member.roles} & {int(o["role_id"]) for o in jload(menu["options"], [])}
        selected = {role_id} if menu["max_values"] == 1 else current | {role_id}
        try:
            await apply_menu_choice(payload.member, menu, selected)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        found = await self._reaction_menu(payload)
        if not found:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        role = guild.get_role(found[1]) if guild else None
        if member and role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------ commands
    role = app_commands.Group(name="role", description="Role management", guild_only=True,
                              default_permissions=discord.Permissions(manage_roles=True))

    async def _can_manage(self, interaction: discord.Interaction, role: discord.Role) -> bool:
        if role >= interaction.guild.me.top_role or role.managed:
            await interaction.response.send_message(embed=err("I can't manage that role (it's above mine or managed by an integration)."), ephemeral=True)
            return False
        if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:  # type: ignore[union-attr]
            await interaction.response.send_message(embed=err("That role is above your highest role."), ephemeral=True)
            return False
        return True

    @role.command(name="add", description="Give a role to a member")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if await self._can_manage(interaction, role):
            await member.add_roles(role, reason=f"By {interaction.user}")
            await self.bot.track_staff(interaction.guild_id, interaction.user.id)
            await interaction.response.send_message(embed=ok(f"Gave {role.mention} to {member.mention}."))

    @role.command(name="remove", description="Remove a role from a member")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if await self._can_manage(interaction, role):
            await member.remove_roles(role, reason=f"By {interaction.user}")
            await self.bot.track_staff(interaction.guild_id, interaction.user.id)
            await interaction.response.send_message(embed=ok(f"Removed {role.mention} from {member.mention}."))

    @role.command(name="temp", description="Give a role temporarily")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def temp(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role, duration: str):
        d = parse_duration(duration)
        if not d:
            return await interaction.response.send_message(embed=err("Invalid duration (e.g. `1d`, `12h`)."), ephemeral=True)
        if not await self._can_manage(interaction, role):
            return
        await member.add_roles(role, reason=f"Temporary role by {interaction.user} for {human_duration(d.total_seconds())}")
        await self.bot.db.execute(
            """INSERT INTO temp_roles (guild_id, user_id, role_id, expires_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id, role_id) DO UPDATE SET expires_at=excluded.expires_at""",
            (interaction.guild_id, member.id, role.id, to_db_time(utcnow() + d)))
        await self.bot.track_staff(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=ok(f"Gave {role.mention} to {member.mention} for {human_duration(d.total_seconds())}."))

    @role.command(name="menu", description="Post or refresh a role menu created on the dashboard")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def menu_cmd(self, interaction: discord.Interaction, menu_id: int):
        row = await self.bot.db.fetchone("SELECT id FROM role_menus WHERE id=? AND guild_id=?", (menu_id, interaction.guild_id))
        if not row:
            return await interaction.response.send_message(embed=err("Menu not found. Create role menus on the dashboard."), ephemeral=True)
        try:
            msg = await self.publish_menu(menu_id)
        except ValueError as exc:
            return await interaction.response.send_message(embed=err(str(exc)), ephemeral=True)
        await interaction.response.send_message(embed=ok(f"Menu posted: {msg.jump_url}"), ephemeral=True)

    @tasks.loop(seconds=60)
    async def temp_loop(self):
        rows = await self.bot.db.fetchall("SELECT * FROM temp_roles WHERE expires_at <= ?", (to_db_time(utcnow()),))
        for r in rows:
            await self.bot.db.execute("DELETE FROM temp_roles WHERE guild_id=? AND user_id=? AND role_id=?", (r["guild_id"], r["user_id"], r["role_id"]))
            guild = self.bot.get_guild(r["guild_id"])
            member = guild.get_member(r["user_id"]) if guild else None
            role = guild.get_role(r["role_id"]) if guild else None
            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Temporary role expired")
                    await log_event(self.bot, guild, "roles", "temp_role_expired", f"Temporary role {role.name} expired for {member}", None, user_id=member.id)
                except discord.HTTPException:
                    pass

    @temp_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Roles(bot))
