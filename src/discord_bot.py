from typing import Union, List, Optional

import discord

from src.constants import Emojis
from src.database import Database
from src.i18n import I18n
from src.model import Products, Programs, UserLanguage, UserState, UserInfo
from src.pretix_cache import PretixCache

MASTER_ERSTI_ROLE_ID = 828216337158766633

PROGRAMS_MAP = {}
PROGRAMS_MAP[Programs.AUTONOMOUS_SYSTEMS] = 813676488909258802
PROGRAMS_MAP[Programs.DISTRIBUTED_SOFTWARE_SYSTEMS] = 813676488876228636
PROGRAMS_MAP[Programs.GENERAL] = 813676488876228637
PROGRAMS_MAP[Programs.INTERNET_AND_WEBBASED_SYSTEMS] = 813676488876228634
PROGRAMS_MAP[Programs.IT_SECURITY] = 813676488876228635
PROGRAMS_MAP[Programs.VISUAL_COMPUTING] = 813676488876228633


class DiscordRole:
    def __init__(self, id: int):
        self.id = id

    def __repr__(self):
        return str(self.id)

    def __str__(self):
        return str(self.id)


class DiscordBot(discord.Client):
    def __init__(self, pretix_cache: PretixCache):
        super().__init__(intents=discord.Intents(members=True, messages=True, reactions=True))

        self._pretix_cache = pretix_cache
        self._database = Database()
        self._i18n = I18n(self._database)

    async def on_ready(self):
        print(f'Discord bot ready as {self.user}.')

        await self.guilds[0].fetch_roles()

    async def on_member_join(self, member: discord.Member):
        discord_username = self._get_discord_username(member)
        user_info = self._database.get_user_info(discord_username)
        if user_info.state == UserState.NEW:
            message = await member.send(self._i18n.get_text(discord_username, 'welcome'))
            await message.add_reaction(Emojis.FLAG_DE)
            await message.add_reaction(Emojis.FLAG_US)

            user_info.next_state()
            user_info.last_message_id = message.id
            self._database.set_user_info(discord_username, user_info)

            self._log(discord_username, user_info, 'Joined.')

    async def on_raw_reaction_add(self, payload):
        user = await self.fetch_user(payload.user_id)
        emoji = payload.emoji.name
        message_id = payload.message_id
        discord_username = self._get_discord_username(user)
        if user.id == self.user.id:
            return
        user_info = self._database.get_user_info(discord_username)
        if message_id != user_info.last_message_id:
            return
        if user_info.state == UserState.LANGUAGE_REQUESTED:
            language, lang_suffix, welcome_filename = None, None, None
            if emoji == Emojis.FLAG_DE:
                language = UserLanguage.GERMAN
                lang_suffix = 'de'
                welcome_filename = 'Willkommen.pdf'
            elif emoji == Emojis.FLAG_US:
                language = UserLanguage.ENGLISH
                lang_suffix = 'en'
                welcome_filename = 'Welcome.pdf'
            if language is not None:
                user_info.next_state()
                user_info.language = language
                self._database.set_user_info(discord_username, user_info)

                await user.send(self._i18n.get_text(discord_username, 'language-set'))

                files = [discord.File(f'files/welcome_{lang_suffix}.pdf', filename=welcome_filename), discord.File(f'files/coc_{lang_suffix}.pdf', filename='Code_of_Conduct.pdf')]
                message = await user.send(self._i18n.get_text(discord_username, 'welcome-coc'), files=files)
                await message.add_reaction(Emojis.YES)
                await message.add_reaction(Emojis.NO)

                user_info.state = UserState.COC_REQUESTED
                user_info.last_message_id = message.id
                self._database.set_user_info(discord_username, user_info)
        elif user_info.state == UserState.COC_REQUESTED:
            coc_accepted = None
            if emoji == Emojis.YES:
                coc_accepted = True
            elif emoji == Emojis.NO:
                coc_accepted = False

            if coc_accepted is not None:
                if coc_accepted:
                    user_info.next_state()
                    self._database.set_user_info(discord_username, user_info)
                    await user.send(self._i18n.get_text(discord_username, 'coc-accepted'))

                    order = self._pretix_cache.get_order(discord_username)
                    if order is None:
                        await self._ask_master_program(discord_username, user, user_info)
                    else:
                        message = await user.send(self._i18n.get_text(discord_username, 'order-found-confirm', product=order.product, programs=order.programs))
                        await message.add_reaction(Emojis.YES)
                        await message.add_reaction(Emojis.NO)
                        user_info.next_state()
                        user_info.last_message_id = message.id
                        self._database.set_user_info(discord_username, user_info)
                else:
                    user_info.state = UserState.COC_DECLINED
                    self._database.set_user_info(discord_username, user_info)
                    await user.send(self._i18n.get_text(discord_username, 'coc-declined'))

                    self._log(discord_username, user_info, 'DECLINED CODE OF CONDUCT! Enabling message log.')
                    self._database.mark_coc_decliner(user.id)
        elif user_info.state == UserState.ORDER_CONFIRM:
            confirmed = None
            if emoji == Emojis.YES:
                confirmed = True
            elif emoji == Emojis.NO:
                confirmed = False
            if confirmed is not None:
                if confirmed:
                    user_info.next_state()
                    self._database.set_user_info(discord_username, user_info)

                    order = self._pretix_cache.get_order(discord_username)
                    if order.product == Products.MASTER:
                        await self._assign_roles(discord_username, user, user_info, order.product, order.programs, False)
                else:
                    await self._ask_master_program(discord_username, user, user_info)
        elif user_info.state == UserState.MANUAL_MASTER_PROGRAM_ASKED:
            await self._handle_master_program_reaction(discord_username, user, user_info, emoji)

    async def on_raw_reaction_remove(self, payload):
        user = await self.fetch_user(payload.user_id)
        emoji = payload.emoji.name
        message_id = payload.message_id
        discord_username = self._get_discord_username(user)
        if user.id == self.user.id:
            return
        user_info = self._database.get_user_info(discord_username)
        if message_id != user_info.last_message_id:
            return
        if user_info.state == UserState.MANUAL_MASTER_PROGRAM_ASKED:
            await self._handle_master_program_reaction(discord_username, user, user_info, emoji, remove=True)

    async def on_message(self, message: discord.Message):
        if self._database.is_coc_decliner(message.author.id):
            print(f'Message from CoC decliner ({message.author}): <{message.content}> ({message.id})')

        if message.content == 'yikes':
            await self.on_member_join(message.author)

    async def _ask_master_program(self, discord_username: str, user: Union[discord.Member, discord.User], user_info: UserInfo):
        message = await user.send(self._i18n.get_text(discord_username, 'ask-master-program'))
        await message.add_reaction(Emojis.ONE)  # AS
        await message.add_reaction(Emojis.TWO)  # DSS
        await message.add_reaction(Emojis.THREE)  # CS
        await message.add_reaction(Emojis.FOUR)  # DKE
        await message.add_reaction(Emojis.FIVE)  # IT-Sec
        await message.add_reaction(Emojis.SIX)  # VC

        user_info.state = UserState.MANUAL_MASTER_PROGRAM_ASKED
        user_info.last_message_id = message.id
        self._database.set_user_info(discord_username, user_info)

    async def _handle_master_program_reaction(self, discord_username: str, user: Union[discord.User, discord.Member], user_info: UserInfo, emoji: str, remove=False):
        program = None
        if emoji == Emojis.ONE:
            program = Programs.AUTONOMOUS_SYSTEMS
        elif emoji == Emojis.TWO:
            program = Programs.DISTRIBUTED_SOFTWARE_SYSTEMS
        elif emoji == Emojis.THREE:
            program = Programs.GENERAL
        elif emoji == Emojis.FOUR:
            program = Programs.INTERNET_AND_WEBBASED_SYSTEMS
        elif emoji == Emojis.FIVE:
            program = Programs.IT_SECURITY
        elif emoji == Emojis.SIX:
            program = Programs.VISUAL_COMPUTING
        if program is not None:
            if remove:
                await self._remove_master_roles(user, [program])
            else:
                await self._assign_roles(discord_username, user, user_info, Products.MASTER, [program], False, finished=False)

    async def _assign_roles(self, discord_username: str, user: Union[discord.User, discord.Member], user_info: UserInfo, product: Optional[Products], programs: Optional[List[Programs]],
                            programming_course: bool, finished=True):
        roles = []
        if product == Products.MASTER:
            roles.append(MASTER_ERSTI_ROLE_ID)
        if programs is not None:
            for program in programs:
                roles.append(PROGRAMS_MAP[program])
        if programming_course:
            roles.append(PROGRAMMING_COURSE_ROLE_ID)

        guild = self.guilds[0]
        member = await guild.fetch_member(user.id)
        await member.add_roles(*[DiscordRole(role_id) for role_id in roles], atomic=True)

        self._log(discord_username, user_info, 'Assigned roles according to product %s, programs %s and pc %s.' % (str(product), str(programs), str(programming_course)))

        if finished:
            user_info.state = UserState.FINISHED
            self._database.set_user_info(discord_username, user_info)
            await user.send(self._i18n.get_text(discord_username, 'roles-assigned'))

            self._log(discord_username, user_info, 'Finished.')

    async def _remove_master_roles(self, user: Union[discord.User, discord.Member], programs: List[Programs]):
        roles = []
        if programs is not None:
            for program in programs:
                roles.append(PROGRAMS_MAP[program])

        guild = self.guilds[0]
        member = await guild.fetch_member(user.id)
        await member.remove_roles(*[DiscordRole(role_id) for role_id in roles], atomic=True)

    def _get_discord_username(self, user: Union[discord.Member, discord.User]):
        return user.name + '#' + user.discriminator

    def _log(self, discord_username: str, user_info: UserInfo, msg: str):
        print('%s, %s: %s' % (discord_username, str(user_info), msg))
