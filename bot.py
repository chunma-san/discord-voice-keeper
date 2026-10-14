"""A dedicated bot that stays silently in one configured voice channel."""
import asyncio
import json
import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'data' / 'state.json'
GUILD_ID = int(os.environ['DISCORD_GUILD_ID'])
EXPECTED_BOT_ID = int(os.environ['EXPECTED_BOT_ID'])


class Keeper(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.lock = asyncio.Lock()
        self.target = None
        self.test_initial_attempted = False
        if STATE.exists():
            self.target = int(json.loads(STATE.read_text())['channel_id'])

    async def setup_hook(self):
        if self.user.id != EXPECTED_BOT_ID:
            raise RuntimeError('Token belongs to a different bot; startup cancelled.')
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.maintain.start()

    def save_target(self, channel_id):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'channel_id': channel_id}))
        tmp.replace(STATE)
        self.target = channel_id

    async def ensure_voice(self):
        async with self.lock:
            if self.target is None:
                return
            guild = self.get_guild(GUILD_ID)
            if guild is None:
                raise RuntimeError('Configured server is unavailable.')
            channel = guild.get_channel(self.target)
            if not isinstance(channel, discord.VoiceChannel):
                raise RuntimeError('Saved voice channel is unavailable.')
            perms = channel.permissions_for(guild.me)
            if not perms.view_channel or not perms.connect:
                raise RuntimeError('View Channel and Connect permissions are required.')
            voice = guild.voice_client
            if voice and voice.is_connected():
                if voice.channel.id != channel.id:
                    await voice.move_to(channel)
                return
            if voice:
                await voice.disconnect(force=True)
            await channel.connect(timeout=30, reconnect=not (ROOT / 'afk-test').exists(), self_mute=True, self_deaf=True)
            logging.info('Connected silently to channel %s', channel.id)

    @tasks.loop(seconds=30)
    async def maintain(self):
        if (ROOT / 'afk-test').exists():
            if self.test_initial_attempted:
                return
            self.test_initial_attempted = True
        try:
            await self.ensure_voice()
        except Exception:
            logging.exception('Voice connection unavailable; retrying in 30 seconds')

    @maintain.before_loop
    async def before_maintain(self):
        await self.wait_until_ready()

    async def on_voice_state_update(self, member, before, after):
        if self.user and member.id == self.user.id:
            logging.getLogger('discord.keeper').info('VOICE_STATE channel=%s -> %s', before.channel.id if before.channel else None, after.channel.id if after.channel else None)

    async def close(self):
        self.maintain.cancel()
        for voice in list(self.voice_clients):
            await voice.disconnect(force=True)
        await super().close()


bot = Keeper()


@bot.tree.command(name='stay', description='Admin: keep this bot in a voice room permanently')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def stay(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if interaction.guild_id != GUILD_ID or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('Administrator permission is required.', ephemeral=True)
        return
    permissions = channel.permissions_for(interaction.guild.me)
    if not permissions.view_channel or not permissions.connect:
        await interaction.response.send_message('Allow the bot to View Channel and Connect first.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with bot.lock:
        bot.save_target(channel.id)
    try:
        await bot.ensure_voice()
        await interaction.followup.send(f'I will stay in {channel.mention}, including when it is empty.', ephemeral=True)
    except Exception:
        logging.exception('Initial voice connection failed')
        await interaction.followup.send('Room saved. Connection failed; I will retry every 30 seconds. Check the service log.', ephemeral=True)


@bot.tree.command(name='status', description='Check the saved room and current connection')
@app_commands.guild_only()
async def status(interaction: discord.Interaction):
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message('This server is not configured.', ephemeral=True)
        return
    voice = interaction.guild.voice_client
    connected = bool(voice and voice.is_connected())
    room = f'<#{bot.target}>' if bot.target else 'Not selected. An administrator must use /stay.'
    await interaction.response.send_message(f'Saved room: {room}\nConnected: {connected}\nAuto-return: {"OFF (AFK test)" if (ROOT / "afk-test").exists() else "every 30 seconds"}.', ephemeral=True)


if __name__ == '__main__':
    bot.run(os.environ['DISCORD_TOKEN'])
