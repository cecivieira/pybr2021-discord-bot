import asyncio
import json
import time
from base64 import b64encode
from datetime import datetime, timedelta

import discord
import httpx
from bot_msg import (auth_instructions, auth_order_not_found,
                     auth_user_not_found)
from decouple import config
from discord import message
from discord.ext import commands, tasks
from discord_setup import get_or_create_channel
from invite_tracker import InviteTracker
from loguru import logger

EVENTBRITE_TOKEN = config("EVENTBRITE_TOKEN")
DISCORD_GUILD_ID = config("DISCORD_GUILD_ID")

INACTIVY_MINUTES_CHECK = config("INACTIVY_MINUTES_CHECK", 15)
FIRST_WARNING_MIN = config("FIRST_WARNING_MIN", 15)
SECOND_WARNING_MIN = config("SECOND_WARNING_MIN", 60)
THIRD_WARNING_MIN = config("FIRST_WARNING_MIN", 120)
KICK_MIN = config("KICK_MIN", 180)

ROLE_INVITE_MAP = [
    ("Ministrantes", ["zuNYMG4jud"]),
    ("Voluntariado", ["j9YH9BqU"]),
    ("Patrocinadoras", ["DfgQhYnVxK"]),
]


async def http_get_json(semaphore, client, url, params, retry=3):
    async with semaphore:
        try:
            response = await client.get(url, params=params)
            return response.json()
        except httpx.ReadTimeout:
            if retry > 0:
                await asyncio.sleep(5)
                return await http_get_json(semaphore, client, url, params, retry - 1)
            logger.exception("Erro")


async def load_attendees(updated_at: datetime = None):
    default_params = {
        "token": EVENTBRITE_TOKEN,
        "status": "attending",
    }
    if updated_at:
        updated_at = updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        default_params["changed_since"] = updated_at

    url = "https://www.eventbriteapi.com/v3/events/169078058023/attendees/"
    semaphore = asyncio.BoundedSemaphore(10)
    async with httpx.AsyncClient() as client:
        response = await http_get_json(semaphore, client, url, default_params)
        if not updated_at:
            logger.info(
                "Attendees load initialized. attendees={attendees}, pages={pages}".format(
                    attendees=response["pagination"]["object_count"],
                    pages=response["pagination"]["page_count"],
                )
            )

        attendees = []
        attendees.extend(response["attendees"])

        tasks = []
        page_count = response["pagination"]["page_count"]
        for page_number in range(1, page_count + 1):
            # I'm not proud of this, but Eventbrite is the one to blame
            next_page = json.dumps({"page": page_number})

            params = default_params.copy()
            params["continuation"] = b64encode(next_page.encode("utf-8")).decode(
                "utf-8"
            )
            tasks.append(http_get_json(semaphore, client, url, params))

        if tasks:
            results = await asyncio.gather(*tasks)
            attendees.extend(
                [attendees for result in results for attendees in result["attendees"]]
            )

    return attendees


def create_index(attendees):
    index = {}
    for attendee in attendees:
        profile = attendee["profile"]
        index[attendee["order_id"]] = profile
        index[profile["email"]] = profile
    return index


class Greetings(commands.Cog):
    CATEGORY_NAME = "Credenciamento"
    WELCOME_CHANNEL_NAME = "boas-vindas"
    ATTENDEES_ROLE_NAME = "Participantes"
    ORG_ROLE_NAME = "Organização"

    def __init__(self, bot):
        self.bot = bot
        self._attendees = []
        self._attendees_updated_at = None
        self._category = None
        self._welcome_channel = None
        self.index = {}
        self.load_indexes.start()
        self.check_inactivity.start()

    @tasks.loop(minutes=1)
    async def load_indexes(self):
        new_attendees = await load_attendees(self._attendees_updated_at)
        if len(new_attendees) != 0:
            logger.info(f"New attendees found. total={len(new_attendees)}")
        self._attendees.extend(new_attendees)
        self._attendees_updated_at = datetime.utcnow()

        self.index = create_index(self._attendees)
        logger.info(
            "Attendees index updated. total={}, updated_at={}".format(
                len(self._attendees),
                self._attendees_updated_at,
            )
        )

    @tasks.loop(minutes=INACTIVY_MINUTES_CHECK)
    async def check_inactivity(self):
        for guild in self.bot.guilds:
            category = await self.get_category(guild)
            now = datetime.utcnow()
            messages = []
            role = await self.get_org_role(guild)
            for channel in category.text_channels:
                channel_diff = (now - channel.created_at).total_seconds() / 60
                if channel_diff >= KICK_MIN:
                    kick_member = guild.get_member(int(channel.name))
                    if kick_member:
                        logger.info(
                            f"Kicking user for inativite on auth={kick_member.name}"
                        )
                        await guild.kick(kick_member)
                    logger.info(f"Removing user auth channel {channel.name}")
                    await channel.delete()
                elif channel_diff >= THIRD_WARNING_MIN:
                    logger.info(f"Third innativite warning {channel.name}")
                    messages.append(
                        channel.send(
                            f"<@{channel.name}>, se você não conseguir confirmar sua inscrição, nós precisaremos remover esse canal para liberar espaço para outras pessoas. Caso isso aconteça, você poderá entrar novamente usando o mesmo link que enviamos por email. Marcando a {role.mention} para ajudarem."
                        )
                    )
                elif channel_diff >= SECOND_WARNING_MIN:
                    logger.info(f"Second innativite warning {channel.name}")
                    messages.append(
                        channel.send(
                            f"<@{channel.name}>, estou avisando a {role.mention} para vir aqui te ajudar!"
                        )
                    )
                elif channel_diff >= FIRST_WARNING_MIN:
                    logger.info(f"First innativite warning {channel.name}")
                    messages.append(
                        channel.send(f"<@{channel.name}>, precisando de ajuda?")
                    )

            await asyncio.gather(*messages)

    @check_inactivity.before_loop
    async def before_check_inactivity(self):
        await self.bot.wait_until_ready()

    def default_permissions_overwrite(self, guild):
        return {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False, read_messages=False
            ),
        }

    async def get_attendee_role(self, guild: discord.Guild) -> discord.Role:
        roles = await guild.fetch_roles()
        return discord.utils.get(roles, name=self.ATTENDEES_ROLE_NAME)

    async def get_org_role(self, guild: discord.Guild) -> discord.Role:
        roles = await guild.fetch_roles()
        return discord.utils.get(roles, name=self.ORG_ROLE_NAME)

    async def get_member(self, guild: discord.Guild, id: int) -> discord.Role:
        members = await guild.fetch_members().flatten()
        return discord.utils.find(lambda m: str(m.id) == id, members)

    async def get_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        if not self._category:
            overwrites = self.default_permissions_overwrite(guild)
            self._category = await get_or_create_channel(
                self.CATEGORY_NAME,
                guild,
                discord.ChannelType.category,
                overwrites=overwrites,
            )
        return self._category

    async def send_auth_instructions(
        self, channel: discord.TextChannel, member: discord.Member
    ):
        await channel.send(auth_instructions.format(name=member.mention))

    async def create_user_auth_channel(
        self, member: discord.Member, category: discord.CategoryChannel
    ):
        org_role = await self.get_org_role(member.guild)
        overwrites = self.default_permissions_overwrite(member.guild)
        overwrites[member] = discord.PermissionOverwrite(read_messages=True)
        overwrites[org_role] = discord.PermissionOverwrite(read_messages=True)

        return await get_or_create_channel(
            str(member.id),
            member.guild,
            category=category,
            overwrites=overwrites,
        )

    @commands.command(name="check-eventbrite")
    async def check_eventbrite(self, ctx, value):
        profile = self.index.get(value)
        if profile:
            message = f"`{value}` encotrado.\n```{profile!r}```"
        else:
            message = f"`{value}` não encotrado."

        await ctx.channel.send(message)

    @commands.Cog.listener()
    async def on_ready(self):
        self.guild = await self.bot.fetch_guild(DISCORD_GUILD_ID)
        self.invite_tracker = InviteTracker(self.bot, self.guild, ROLE_INVITE_MAP)
        await self.invite_tracker.sync()
        logger.info(f"Invite tracker synced. invites={self.invite_tracker.invites!r}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        joined_with_invite_code = await self.invite_tracker.check_new_user(member)
        if joined_with_invite_code:
            return

        guild = member.guild
        category = await self.get_category(member.guild)
        channel = await self.create_user_auth_channel(member, category)

        await self.send_auth_instructions(channel, member)

    def should_authenticate_user(self, message: discord.Message):
        channel = message.channel
        author = message.author
        checks = [
            (
                channel.type == discord.ChannelType.text
                and channel.name == str(author.id)
            ),
            (
                not author.bot
                and getattr(author, "roles", False)
                and len(author.roles) == 1
                and author.roles[0].is_default()
            ),
            (
                getattr(channel, "category", False)
                and channel.category.name == self.CATEGORY_NAME
            ),
        ]
        return all(checks)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.should_authenticate_user(message):
            return

        logger.info(
            f"Authenticating user. user={message.author.name}, id={message.author.id}, content={message.content}"
        )

        # TODO validar se usuário já está inscrito

        profile = self.index.get(message.content)
        if not profile:
            logger.info(
                f"User not found on index. user_id={message.author.id}, content={message.content!r}"
            )
            role = await self.get_org_role(message.guild)
            # TODO temporay remnoving
            # await message.channel.send(
            #     content=auth_order_not_found.format(role=role.mention)
            # )
            return

        member = await self.get_member(message.guild, message.channel.name)
        if not member:
            logger.warning(
                f"User with channel's name not found on Discord. channel={message.channel.name}"
            )
            await message.channel.send(
                content=auth_user_not_found.format(id=message.channel.name)
            )
            return

        role = await self.get_attendee_role(message.guild)
        await member.add_roles(role)
        await message.channel.delete()
        logger.info(
            f"User authenticated and channel deleted. user={message.author.name}"
        )
