import discord
from discord.ext import commands
from discord import app_commands
import os
import sys
import asyncio
import datetime
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

# --- PRE-FLIGHT CHECKS ---
REQUIRED_VARS = ["DISCORD_TOKEN", "MONGO_URL", "OWNER_ID", "GEMINI_API_KEY"]
missing_vars = [var for var in REQUIRED_VARS if not os.getenv(var)]
if missing_vars:
    print(f"FATAL: Missing Environment Variables: {', '.join(missing_vars)}")
    sys.exit(1)

try:
    OWNER_ID = int(os.getenv("OWNER_ID"))
except ValueError:
    print("FATAL: OWNER_ID must be an integer.")
    sys.exit(1)

class YuriBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix='!',
            intents=intents,
            help_command=None,
            activity=discord.Activity(type=discord.ActivityType.listening, name="startup...")
        )
        self.owner_id = OWNER_ID

    async def setup_hook(self):
        # Database Setup
        mongo_url = os.getenv("MONGO_URL")
        try:
            self.mongo = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
            await self.mongo.admin.command('ping')
            self.db = self.mongo["yuri_bot_db"]
            print("MongoDB Connected.")
        except Exception as e:
            print(f"FATAL: MongoDB Connection Failed: {e}")
            sys.exit(1)

        # Attach collections globally
        self.chat_collection = self.db["chat_history"]
        self.config_collection = self.db["server_configs"]
        self.crush_collection = self.db["crushes"]
        self.grudge_collection = self.db["grudges"]
        self.feedback_collection = self.db["feedback"]

        try:
            # Create Indexes
            await self.chat_collection.create_index("timestamp", expireAfterSeconds=2592000)
            await self.chat_collection.create_index("user_id")  # Fast user history lookups at scale
            await self.crush_collection.create_index([("lover_id", 1), ("target_id", 1)], unique=True)
            await self.grudge_collection.create_index("user_id", unique=True)
        except Exception as e:
            print(f"Index Creation Warning: {e}")

        # Load Cogs
        initial_extensions = ["cogs.ai", "cogs.social", "cogs.admin", "cogs.general"]
        for extension in initial_extensions:
            try:
                await self.load_extension(extension)
            except Exception as e:
                print(f"Failed to load extension {extension}: {e}")

        print("Bot is ready to serve.")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Global slash command error handler — sends friendly developer embed to user."""
        # Unwrap the original error if wrapped
        error = getattr(error, "original", error)

        # Log to Railway console
        cmd = interaction.command.name if interaction.command else "unknown"
        print(f"[SLASH ERROR] /{cmd} by {interaction.user} ({interaction.user.id}): {type(error).__name__}: {error}")

        # Match error to specific message + solution
        if isinstance(error, app_commands.MissingPermissions):
            title = "Permission Denied"
            description = "You don't have the required permissions to use this command. Please ask a server administrator for help."

        elif isinstance(error, app_commands.BotMissingPermissions):
            missing = ", ".join(f"`{p}`" for p in error.missing_permissions)
            title = "Bot Missing Permissions"
            description = f"I'm missing the following permissions to run this command: {missing}\n\nPlease ask a server admin to update my role permissions."

        elif isinstance(error, discord.Forbidden):
            title = "Action Blocked by Discord"
            description = "I don't have permission to do that. This is usually a role hierarchy issue — make sure my role is positioned above the target user's role in Server Settings > Roles."

        elif isinstance(error, discord.HTTPException):
            title = "Discord Error"
            description = "Discord had a temporary hiccup while processing this request. Please wait a moment and try again."

        elif isinstance(error, app_commands.CommandOnCooldown):
            title = "Slow Down!"
            description = f"This command is on cooldown. Please try again in **{error.retry_after:.1f} seconds**."

        elif isinstance(error, app_commands.NoPrivateMessage):
            title = "Server Only"
            description = "This command can only be used inside a server, not in DMs."

        elif isinstance(error, app_commands.MissingRole):
            title = "Missing Role"
            description = f"You need the **{error.missing_role}** role to use this command."

        elif isinstance(error, asyncio.TimeoutError):
            title = "Request Timed Out"
            description = "The command took too long to process and timed out. Please try again."

        else:
            title = "Something Went Wrong"
            description = "An unexpected error occurred. If this keeps happening, please report it using `/feedback` so we can look into it."

        embed = discord.Embed(
            title=f"🛠️ {title}",
            description=description,
            color=discord.Color.from_rgb(255, 80, 80),
            timestamp=datetime.datetime.utcnow()
        )
        embed.set_footer(text="Developer • Yuri Bot Support")

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"[ERROR HANDLER FAILED]: {e}")

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

if __name__ == "__main__":
    bot = YuriBot()

    @bot.command()
    @commands.is_owner()
    async def sync(ctx):
        synced = await ctx.bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} slash commands.")

    bot.run(os.getenv('DISCORD_TOKEN'))
    
